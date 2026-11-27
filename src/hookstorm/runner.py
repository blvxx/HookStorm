"""Async webhook scenario runner."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import os
import time
import uuid
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpcore
import httpx

from hookstorm import __version__
from hookstorm.config import scenario_fingerprint
from hookstorm.safety import TargetResolution, resolve_target
from hookstorm.standard_webhooks import (
    decode_standard_webhooks_secret,
    standard_webhooks_headers,
)
from hookstorm.verify_auth import (
    resolve_verification_auth_headers,
    validate_verification_auth_transport,
)


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _match_json_value(actual: Any, expected: Any, path: str) -> tuple[bool, str | None]:
    """Match nested JSON objects as subsets and arrays as exact ordered values."""
    if isinstance(expected, dict):
        if not isinstance(actual, dict):
            return False, f"response JSON value '{path}' is not an object"
        for key, expected_value in expected.items():
            child_path = f"{path}.{key}" if path else str(key)
            if key not in actual:
                return False, f"response JSON is missing key '{child_path}'"
            matched, mismatch = _match_json_value(actual[key], expected_value, child_path)
            if not matched:
                return False, mismatch
        return True, None

    if isinstance(expected, list):
        if not isinstance(actual, list):
            return False, f"response JSON value '{path}' is not an array"
        if len(actual) != len(expected):
            return False, f"response JSON array '{path}' has a different length"
        for index, (actual_item, expected_item) in enumerate(zip(actual, expected, strict=True)):
            matched, mismatch = _match_json_value(actual_item, expected_item, f"{path}[{index}]")
            if not matched:
                return False, mismatch
        return True, None

    # Python considers True == 1; JSON booleans and numbers must remain distinct.
    if isinstance(expected, bool):
        matched = isinstance(actual, bool) and actual is expected
    elif isinstance(expected, (int, float)):
        matched = not isinstance(actual, bool) and isinstance(actual, (int, float)) and actual == expected
    else:
        matched = type(actual) is type(expected) and actual == expected

    if not matched:
        return False, f"response JSON value '{path}' did not match the expected value"
    return True, None


def _matches_subset(actual: Any, expected: dict[str, Any]) -> tuple[bool, str | None]:
    if not isinstance(actual, dict):
        return False, "response body is not a JSON object"
    return _match_json_value(actual, expected, "")


class _PinnedNetworkBackend:
    """Route hostnames only to the IP addresses validated before the run."""

    def __init__(
        self,
        backend: Any,
        pinned_addresses: dict[str, tuple[str, ...]],
    ) -> None:
        self._backend = backend
        self._pinned_addresses = pinned_addresses

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Any = None,
    ) -> Any:
        addresses = self._pinned_addresses.get(host.rstrip(".").lower())
        if addresses is None:
            return await self._backend.connect_tcp(
                host,
                port,
                timeout=timeout,
                local_address=local_address,
                socket_options=socket_options,
            )

        failures: list[Exception] = []
        for address in addresses:
            try:
                return await self._backend.connect_tcp(
                    address,
                    port,
                    timeout=timeout,
                    local_address=local_address,
                    socket_options=socket_options,
                )
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as exc:
                failures.append(exc)

        raise failures[-1]

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Any = None,
    ) -> Any:
        return await self._backend.connect_unix_socket(
            path,
            timeout=timeout,
            socket_options=socket_options,
        )

    async def sleep(self, seconds: float) -> None:
        await self._backend.sleep(seconds)


class _PartialResponseReadErrorStream(httpx.AsyncByteStream):
    """Yield part of a response body, then simulate a connection reset."""

    def __init__(self, stream: httpx.AsyncByteStream) -> None:
        self._stream = stream

    async def __aiter__(self):
        async for chunk in self._stream:
            if chunk:
                yield chunk[: max(1, len(chunk) // 2)]
                raise httpx.ReadError(
                    "injected connection reset after a partial response body"
                )
        raise httpx.ReadError(
            "injected connection reset before the response body completed"
        )

    async def aclose(self) -> None:
        await self._stream.aclose()


class _FaultInjectingTransport(httpx.AsyncBaseTransport):
    """Apply deterministic faults and pin HTTPX DNS to pre-validated addresses."""

    def __init__(self, targets: tuple[TargetResolution, ...]) -> None:
        self._transport = httpx.AsyncHTTPTransport(trust_env=False)
        pinned_addresses: dict[str, list[str]] = {}
        for target in targets:
            values = pinned_addresses.setdefault(target.hostname, [])
            for address in target.addresses:
                if address not in values:
                    values.append(address)

        # HTTPX delegates socket creation to its httpcore pool. Replace only
        # the network backend: the original URL host remains intact for the
        # HTTP Host header, TLS SNI, and certificate hostname verification.
        pool = getattr(self._transport, "_pool", None)
        backend = getattr(pool, "_network_backend", None)
        if pool is None or backend is None:
            raise RuntimeError(
                "The installed HTTPX transport does not expose the network "
                "backend needed to pin validated DNS results"
            )
        pool._network_backend = _PinnedNetworkBackend(
            backend,
            {hostname: tuple(addresses) for hostname, addresses in pinned_addresses.items()},
        )

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        fault = request.extensions.get("hookstorm_fault_injection")
        if fault == "connect_error":
            raise httpx.ConnectError(
                "injected connection failure before request reached the endpoint",
                request=request,
            )

        response = await self._transport.handle_async_request(request)
        if fault == "response_loss":
            await response.aclose()
            raise httpx.ReadError(
                "injected acknowledgement loss after the endpoint processed the request",
                request=request,
            )
        if fault == "response_body_error":
            response.stream = _PartialResponseReadErrorStream(response.stream)
        return response

    async def aclose(self) -> None:
        await self._transport.aclose()


MAX_RESPONSE_BODY_BYTES = 2 * 1024 * 1024


async def _read_limited_response_body(response: httpx.Response) -> bytes | None:
    """Read a decompressed response body up to the fixed memory-safety limit.

    Return None when the body exceeds the limit. Streaming is used so an oversized
    response is closed as soon as the limit is crossed instead of being buffered in full.
    """
    body = bytearray()
    async for chunk in response.aiter_bytes():
        if len(body) + len(chunk) > MAX_RESPONSE_BODY_BYTES:
            return None
        body.extend(chunk)
    return bytes(body)


_SIGNATURE_HEADERS = {
    "generic": "X-HookStorm-Signature",
    "stripe": "Stripe-Signature",
    "github": "X-Hub-Signature-256",
    "shopify": "X-Shopify-Hmac-Sha256",
}


def _signature_headers(
    body: bytes,
    secret: str,
    mode: str,
    provider: str,
    event_id: str,
    timestamp_offset_seconds: int = 0,
) -> dict[str, str]:
    """Build provider-compatible signature headers for one individual delivery attempt."""
    if mode in {"none", "missing"}:
        return {}

    timestamp: int | None = None
    if provider == "standard_webhooks":
        timestamp = int(time.time()) + timestamp_offset_seconds
        return standard_webhooks_headers(
            secret,
            event_id,
            body,
            timestamp,
            invalid=mode == "invalid",
        )

    signed_payload = body
    if provider == "stripe":
        timestamp = int(time.time()) + timestamp_offset_seconds
        signed_payload = str(timestamp).encode("ascii") + b"." + body

    digest = bytearray(
        hmac.new(secret.encode("utf-8"), signed_payload, hashlib.sha256).digest()
    )
    if mode == "invalid":
        digest[0] ^= 0x01
    digest_bytes = bytes(digest)

    if provider == "shopify":
        value = base64.b64encode(digest_bytes).decode("ascii")
    else:
        hex_digest = digest_bytes.hex()
        if provider == "stripe":
            value = f"t={timestamp},v1={hex_digest}"
        else:
            value = f"sha256={hex_digest}"

    return {_SIGNATURE_HEADERS[provider]: value}


async def run_scenario(
    scenario: dict[str, Any],
    target: str,
    *,
    allow_remote: bool = False,
    allow_insecure_http: bool = False,
    secret: str | None = None,
) -> dict[str, Any]:
    """Execute a validated scenario and return a privacy-conscious result document."""
    target_resolution = resolve_target(
        target,
        allow_remote=allow_remote,
        allow_insecure_http=allow_insecure_http,
    )
    target = target_resolution.url
    config_fingerprint = scenario_fingerprint(scenario)
    verify_config = scenario.get("verify")
    verification_auth_config = verify_config.get("auth") if verify_config else None
    if verify_config is not None and verification_auth_config is not None:
        validate_verification_auth_transport(
            verify_config["url"], verification_auth_config
        )
    verification_resolution = (
        resolve_target(
            verify_config["url"],
            allow_remote=allow_remote,
            allow_insecure_http=allow_insecure_http,
        )
        if verify_config is not None
        else None
    )
    verification_url = (
        verification_resolution.url if verification_resolution is not None else None
    )
    # Resolve verification credentials before any webhook event is sent.
    verification_auth_headers = resolve_verification_auth_headers(verification_auth_config)
    secret = secret if secret is not None else os.getenv("HOOKSTORM_SECRET")
    started = datetime.now(UTC)
    monotonic_started = time.perf_counter()
    run_id = uuid.uuid4().hex
    target_parts = urlsplit(target)
    # Query strings often contain API keys or signed tokens; never copy them into reports.
    reported_target = urlunsplit(
        (target_parts.scheme, target_parts.netloc, target_parts.path, "", "")
    )
    if verification_url is not None:
        verify_parts = urlsplit(verification_url)
        reported_verify_target = urlunsplit(
            (verify_parts.scheme, verify_parts.netloc, verify_parts.path, "", "")
        )
    else:
        reported_verify_target = None
    all_deliveries: list[dict[str, Any]] = []
    default_delivery = {
        "order": "in_order",
        "duplicate_count": 0,
        "delay_ms": 0,
        "timeout_ms": 5_000,
        "signature": "none",
        "signature_provider": "generic",
        "signature_timestamp_offset_seconds": 0,
        "concurrency": 1,
        "fault_injection": {
            "connect_error_attempts": [],
            "response_loss_attempts": [],
            "response_body_error_attempts": [],
        },
    }
    retry = scenario["retry"]
    concurrency = scenario["delivery"].get("concurrency", 1)
    semaphore = asyncio.Semaphore(concurrency)
    ordered_events = list(scenario["events"])
    if scenario["delivery"].get("order", "in_order") == "reverse":
        ordered_events.reverse()

    # Fail before sending any traffic if a signature mode needs a missing or malformed secret.
    for event in ordered_events:
        effective_delivery = {**default_delivery, **scenario["delivery"], **event["delivery"]}
        if effective_delivery["signature"] not in {"valid", "invalid"}:
            continue
        if not secret:
            raise ValueError(
                "This scenario requires HOOKSTORM_SECRET or the configured secret environment variable."
            )
        if effective_delivery["signature_provider"] == "standard_webhooks":
            decode_standard_webhooks_secret(secret)

    async def process_event(event: dict[str, Any]) -> list[dict[str, Any]]:
        scheduled_at = monotonic_started + event.get("schedule_after_ms", 0) / 1000
        schedule_wait = scheduled_at - time.perf_counter()
        if schedule_wait > 0:
            await asyncio.sleep(schedule_wait)

        async with semaphore:
            delivery_config = {**default_delivery, **scenario["delivery"], **event["delivery"]}
            event_body = _json_bytes(event["payload"])
            base_headers = {
                "Content-Type": "application/json",
                "User-Agent": f"HookStorm/{__version__} (+https://github.com/blvxx/HookStorm)",
                "X-HookStorm-Run": run_id,
                "X-Event-ID": event["event_id"],
                "X-Event-Type": event["id"],
            }
            reserved = {
                "x-hookstorm-run",
                "x-event-id",
                "x-event-type",
                "x-hookstorm-attempt",
                "x-hookstorm-signature",
                "x-hub-signature-256",
                "stripe-signature",
                "x-shopify-hmac-sha256",
                "x-hookstorm-duplicate-index",
                "webhook-id",
                "webhook-timestamp",
                "webhook-signature",
            }
            for key, value in event["headers"].items():
                if key.lower() not in reserved:
                    base_headers[key] = value

            event_deliveries: list[dict[str, Any]] = []
            for duplicate_index in range(delivery_config["duplicate_count"] + 1):
                attempts: list[dict[str, Any]] = []
                expected_json = (
                    event["expect_duplicate_json"]
                    if duplicate_index > 0 and event["expect_duplicate_json"] is not None
                    else event["expect_json"]
                )
                for attempt in range(1, retry["max_attempts"] + 1):
                    if delivery_config["delay_ms"]:
                        await asyncio.sleep(delivery_config["delay_ms"] / 1000)
                    headers = dict(base_headers)
                    headers["X-HookStorm-Attempt"] = str(attempt)
                    headers["X-HookStorm-Duplicate-Index"] = str(duplicate_index)
                    headers.update(
                        _signature_headers(
                            event_body,
                            secret or "",
                            delivery_config["signature"],
                            delivery_config["signature_provider"],
                            event["event_id"],
                            delivery_config["signature_timestamp_offset_seconds"],
                        )
                    )
                    started_attempt = time.perf_counter()
                    fault_plan = delivery_config.get(
                        "fault_injection",
                        {
                            "connect_error_attempts": [],
                            "response_loss_attempts": [],
                            "response_body_error_attempts": [],
                        },
                    )
                    injected_fault = (
                        "connect_error"
                        if attempt in fault_plan.get("connect_error_attempts", [])
                        else "response_loss"
                        if attempt in fault_plan.get("response_loss_attempts", [])
                        else "response_body_error"
                        if attempt in fault_plan.get("response_body_error_attempts", [])
                        else None
                    )
                    outcome: dict[str, Any] = {
                        "attempt": attempt,
                        "elapsed_ms": round((started_attempt - monotonic_started) * 1000, 2),
                        "status_code": None,
                        "latency_ms": 0.0,
                        "error": None,
                        "expected_status": False,
                        "expected_json": expected_json is None,
                        "passed": False,
                    }
                    if injected_fault is not None:
                        outcome["injected_fault"] = injected_fault
                    try:
                        async with client.stream(
                            "POST",
                            target,
                            content=event_body,
                            headers=headers,
                            timeout=delivery_config["timeout_ms"] / 1000,
                            extensions=(
                                {"hookstorm_fault_injection": injected_fault}
                                if injected_fault is not None
                                else None
                            ),
                        ) as response:
                            outcome["status_code"] = response.status_code
                            response_body = await _read_limited_response_body(response)
                            outcome["latency_ms"] = round(
                                (time.perf_counter() - started_attempt) * 1000, 2
                            )
                            outcome["expected_status"] = (
                                response.status_code in event["expect_status"]
                            )
                            if response_body is None:
                                outcome["expected_json"] = False
                                outcome["error"] = (
                                    "response body exceeds the 2 MiB safety limit"
                                )
                            elif expected_json is not None:
                                try:
                                    matched, mismatch = _matches_subset(
                                        json.loads(response_body), expected_json
                                    )
                                except ValueError:
                                    matched, mismatch = (
                                        False,
                                        "response body is not valid JSON",
                                    )
                                outcome["expected_json"] = matched
                                if mismatch:
                                    outcome["error"] = mismatch
                            outcome["passed"] = (
                                outcome["expected_status"] and outcome["expected_json"]
                            )
                            retryable = response.status_code in retry["retry_on"]
                    except httpx.TimeoutException:
                        outcome["latency_ms"] = round(
                            (time.perf_counter() - started_attempt) * 1000, 2
                        )
                        outcome["error"] = "request timed out"
                        retryable = True
                    except httpx.RequestError as exc:
                        outcome["latency_ms"] = round(
                            (time.perf_counter() - started_attempt) * 1000, 2
                        )
                        if injected_fault == "connect_error":
                            outcome["error"] = (
                                "injected connection failure before request reached the endpoint"
                            )
                        elif injected_fault == "response_loss":
                            outcome["error"] = (
                                "injected acknowledgement loss after the endpoint processed the request"
                            )
                        elif injected_fault == "response_body_error":
                            outcome["error"] = (
                                "injected connection reset after a partial response body"
                            )
                        else:
                            outcome["error"] = f"request error: {type(exc).__name__}"
                        retryable = True
                    attempts.append(outcome)
                    if not retryable or attempt >= retry["max_attempts"]:
                        break
                    if retry["backoff_ms"]:
                        await asyncio.sleep(
                            min(retry["backoff_ms"] * (2 ** (attempt - 1)), 60_000) / 1000
                        )

                last = attempts[-1]
                event_deliveries.append(
                    {
                        "event_id": event["event_id"],
                        "event_type": event["id"],
                        "duplicate_index": duplicate_index,
                        "schedule_after_ms": event.get("schedule_after_ms", 0),
                        "signature_provider": delivery_config["signature_provider"],
                        "attempts": attempts,
                        "passed": bool(last["passed"]),
                    }
                )
            return event_deliveries

    try:
        async with asyncio.timeout(scenario["run_timeout_ms"] / 1000):
            async with httpx.AsyncClient(
                follow_redirects=False,
                trust_env=False,
                transport=_FaultInjectingTransport(
                    (target_resolution,)
                    if verification_resolution is None
                    else (target_resolution, verification_resolution)
                ),
                timeout=httpx.Timeout(30.0, connect=5.0),
            ) as client:
                # Start the earliest scheduled events first, even if setup overhead means
                # multiple offsets have already elapsed. Restore scenario order after execution.
                execution_order = sorted(
                    range(len(ordered_events)),
                    key=lambda index: ordered_events[index].get("schedule_after_ms", 0),
                )
                scheduled_groups = await asyncio.gather(
                    *(process_event(ordered_events[index]) for index in execution_order)
                )
                groups_by_scenario_index = {
                    index: group
                    for index, group in zip(execution_order, scheduled_groups, strict=True)
                }
                all_deliveries = [
                    delivery
                    for index in range(len(ordered_events))
                    for delivery in groups_by_scenario_index[index]
                ]

                verification: dict[str, Any] | None = None
                if verify_config is not None and verification_url is not None:
                    verify_started = time.perf_counter()
                    verification = {
                        "method": "GET",
                        "target": reported_verify_target,
                        "status_code": None,
                        "latency_ms": 0.0,
                        "error": None,
                        "expected_status": False,
                        "expected_json": False,
                        "passed": False,
                    }
                    try:
                        async with client.stream(
                            "GET",
                            verification_url,
                            headers={
                                "User-Agent": f"HookStorm/{__version__} (+https://github.com/blvxx/HookStorm)",
                                "X-HookStorm-Run": run_id,
                                **verification_auth_headers,
                            },
                            timeout=verify_config["timeout_ms"] / 1000,
                        ) as response:
                            verification["status_code"] = response.status_code
                            response_body = await _read_limited_response_body(response)
                            verification["latency_ms"] = round(
                                (time.perf_counter() - verify_started) * 1000, 2
                            )
                            verification["expected_status"] = (
                                response.status_code in verify_config["expect_status"]
                            )
                            matched: bool
                            mismatch: str | None
                            if response_body is None:
                                matched, mismatch = (
                                    False,
                                    "verification response body exceeds the 2 MiB safety limit",
                                )
                            else:
                                try:
                                    matched, mismatch = _matches_subset(
                                        json.loads(response_body), verify_config["expect_json"]
                                    )
                                except ValueError:
                                    matched, mismatch = (
                                        False,
                                        "verification response body is not valid JSON",
                                    )
                        verification["expected_json"] = matched
                        if mismatch:
                            verification["error"] = mismatch
                        elif not verification["expected_status"]:
                            verification["error"] = (
                                f"verification returned unexpected HTTP status {response.status_code}"
                            )
                        verification["passed"] = (
                            verification["expected_status"] and verification["expected_json"]
                        )
                    except httpx.TimeoutException:
                        verification["latency_ms"] = round(
                            (time.perf_counter() - verify_started) * 1000, 2
                        )
                        verification["error"] = "verification request timed out"
                    except httpx.RequestError as exc:
                        verification["latency_ms"] = round(
                            (time.perf_counter() - verify_started) * 1000, 2
                        )
                        verification["error"] = (
                            f"verification request error: {type(exc).__name__}"
                        )


    except TimeoutError as exc:
        raise ValueError(
            f"Scenario exceeded run_timeout_ms={scenario['run_timeout_ms']}; "
            "in-flight requests were cancelled"
        ) from exc

    passed = sum(1 for delivery in all_deliveries if delivery["passed"])
    verification_passed = verification is None or bool(verification["passed"])
    return {
        "schema_version": 1,
        "tool": "HookStorm",
        "version": __version__,
        "run_id": run_id,
        "scenario": scenario["name"],
        "scenario_fingerprint": config_fingerprint,
        "target": reported_target,
        "started_at": started.isoformat(),
        "duration_ms": round((time.perf_counter() - monotonic_started) * 1000, 2),
        "summary": {
            "total_deliveries": len(all_deliveries),
            "passed": passed,
            "failed": len(all_deliveries) - passed,
            "attempts": sum(len(delivery["attempts"]) for delivery in all_deliveries),
            "success": passed == len(all_deliveries) and verification_passed,
        },
        "deliveries": all_deliveries,
        "verification": verification,
    }
