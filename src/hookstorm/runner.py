"""Async webhook scenario runner."""

from __future__ import annotations

import asyncio
import base64
import contextvars
import gzip
import hashlib
import hmac
import json
import os
import time
import uuid
import zlib
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any, cast
from urllib.parse import urlsplit, urlunsplit

import httpcore
import httpx

from hookstorm import __version__
from hookstorm.config import scenario_fingerprint
from hookstorm.json_limits import ensure_json_text_depth
from hookstorm.safety import (
    TargetResolution,
    UnsafeTargetError,
    canonicalize_hostname,
    resolve_target,
)
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


def _parse_response_json(body: bytes) -> Any:
    """Parse bounded UTF-8 JSON without ambiguity, pathological nesting, or non-finite numbers."""

    def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise ValueError(f"response JSON contains duplicate object key {key!r}")
            value[key] = item
        return value

    try:
        source = body.decode("utf-8")
        ensure_json_text_depth(source)
        value = json.loads(source, object_pairs_hook=reject_duplicate_keys)
        # The standard decoder accepts NaN/Infinity and converts 1e999 to inf.
        # Reject those values even when a caller only checks a subset of fields.
        json.dumps(value, ensure_ascii=True, allow_nan=False)
        return value
    except (UnicodeDecodeError, ValueError, RecursionError, OverflowError) as exc:
        raise ValueError("response body is not valid bounded UTF-8 JSON") from exc


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


WIRE_REQUEST_FAULTS = frozenset(
    {
        "chunked_transfer",
        "malformed_chunked",
        "partial_upload",
        "content_length_mismatch",
    }
)
_REQUEST_FAULT_CONTEXT: contextvars.ContextVar[tuple[str, int] | None] = (
    contextvars.ContextVar("hookstorm_request_fault", default=None)
)


def prepare_request_body(body: bytes, fault: str | None) -> tuple[bytes, dict[str, str]]:
    """Apply request payload/content-encoding variants before signature generation."""
    if fault == "malformed_json":
        return b"!" + body, {}
    if fault == "gzip_body":
        return gzip.compress(body, mtime=0), {"Content-Encoding": "gzip"}
    if fault == "deflate_body":
        return zlib.compress(body), {"Content-Encoding": "deflate"}
    if fault == "invalid_gzip":
        return b"\x1f\x8b\x08\x00hookstorm-invalid-gzip", {
            "Content-Encoding": "gzip"
        }
    if fault == "invalid_deflate":
        return b"\x78\x9chookstorm-invalid-deflate", {
            "Content-Encoding": "deflate"
        }
    if fault == "wrong_content_type":
        return body, {"Content-Type": "text/plain"}
    return body, {}


def _rewrite_request_headers(header_block: bytes, fault: str, body_length: int) -> bytes:
    lines = header_block.split(b"\r\n")
    request_line, raw_headers = lines[0], lines[1:]
    headers: list[bytes] = []
    replaced_length = False

    for line in raw_headers:
        if not line:
            continue
        name, separator, _value = line.partition(b":")
        if not separator:
            headers.append(line)
            continue
        lowered = name.strip().lower()
        if fault == "chunked_transfer" and lowered in {
            b"content-length",
            b"transfer-encoding",
        }:
            continue
        if fault == "malformed_chunked" and lowered == b"transfer-encoding":
            continue
        if fault == "content_length_mismatch" and lowered == b"content-length":
            headers.append(f"Content-Length: {body_length + 1}".encode("ascii"))
            replaced_length = True
            continue
        if fault in {"partial_upload", "content_length_mismatch"} and lowered == b"connection":
            headers.append(b"Connection: close")
            continue
        headers.append(line)

    if fault in {"chunked_transfer", "malformed_chunked"}:
        headers.append(b"Transfer-Encoding: chunked")
    if fault == "malformed_chunked" and not any(
        line.partition(b":")[0].strip().lower() == b"content-length"
        for line in headers
    ):
        headers.append(f"Content-Length: {body_length}".encode("ascii"))
    if fault == "content_length_mismatch" and not replaced_length:
        headers.append(f"Content-Length: {body_length + 1}".encode("ascii"))

    if fault in {"partial_upload", "content_length_mismatch"}:
        headers.append(b"Connection: close")

    return b"\r\n".join([request_line, *headers, b"", b""])


class _RequestFaultNetworkStream:
    """Mutate HTTP/1.1 body framing after httpcore has serialized the request."""

    def __init__(self, stream: Any, fault: str, body_length: int) -> None:
        self._stream = stream
        self._fault = fault
        self._body_length = body_length
        self._headers_sent = False
        self._body_bytes_sent = 0
        self._closed = False
        self._pending_headers = bytearray()

    async def write(self, buffer: bytes, timeout: float | None = None) -> None:
        if self._closed:
            raise httpcore.WriteError("request fault closed the network stream")
        if not buffer:
            return

        if not self._headers_sent:
            self._pending_headers.extend(buffer)
            marker = b"\r\n\r\n"
            header_bytes = bytes(self._pending_headers)
            split_at = header_bytes.find(marker)
            if split_at < 0:
                return
            header_block = header_bytes[:split_at]
            remainder = header_bytes[split_at + len(marker):]
            rewritten = _rewrite_request_headers(
                header_block,
                self._fault,
                self._body_length,
            )
            await self._stream.write(rewritten, timeout=timeout)
            self._headers_sent = True
            self._pending_headers.clear()
            if remainder:
                await self._write_body(remainder, timeout)
            return

        await self._write_body(buffer, timeout)

    async def _write_body(self, buffer: bytes, timeout: float | None) -> None:
        if self._fault == "chunked_transfer":
            self._body_bytes_sent += len(buffer)
            chunk = f"{len(buffer):X}\r\n".encode("ascii") + buffer + b"\r\n"
            if self._body_bytes_sent >= self._body_length:
                chunk += b"0\r\n\r\n"
            await self._stream.write(chunk, timeout=timeout)
            return

        if self._fault == "malformed_chunked":
            if self._body_bytes_sent == 0:
                self._body_bytes_sent += len(buffer)
                await self._stream.write(
                    b"Z\r\n" + buffer + b"\r\n0\r\n\r\n",
                    timeout=timeout,
                )
            return

        if self._fault == "partial_upload":
            partial_length = max(0, self._body_length // 2)
            remaining = max(0, partial_length - self._body_bytes_sent)
            if remaining:
                await self._stream.write(buffer[:remaining], timeout=timeout)
                self._body_bytes_sent += min(len(buffer), remaining)
            await self._close_and_fail("injected partial request upload")
            return

        if self._fault == "content_length_mismatch":
            await self._stream.write(buffer, timeout=timeout)
            self._body_bytes_sent += len(buffer)
            if self._body_bytes_sent >= self._body_length:
                await self._close_and_fail(
                    "injected Content-Length larger than the transmitted request body"
                )
            return

        await self._stream.write(buffer, timeout=timeout)

    async def _close_and_fail(self, message: str) -> None:
        self._closed = True
        await self._stream.aclose()
        raise httpcore.WriteError(message)

    async def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        return await self._stream.read(max_bytes, timeout=timeout)

    async def start_tls(
        self,
        ssl_context: Any,
        server_hostname: str | None = None,
        timeout: float | None = None,
    ) -> _RequestFaultNetworkStream:
        self._stream = await self._stream.start_tls(
            ssl_context,
            server_hostname=server_hostname,
            timeout=timeout,
        )
        return self

    def get_extra_info(self, info: str) -> Any:
        return self._stream.get_extra_info(info)

    async def aclose(self) -> None:
        if not self._closed:
            self._closed = True
            await self._stream.aclose()


def _wrap_request_fault_stream(stream: Any) -> Any:
    context = _REQUEST_FAULT_CONTEXT.get()
    if context is None:
        return stream
    fault, body_length = context
    return _RequestFaultNetworkStream(stream, fault, body_length)


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
        try:
            canonical_host = canonicalize_hostname(host)
        except UnsafeTargetError as exc:
            raise httpcore.ConnectError(
                "HTTP transport requested an invalid hostname after target validation"
            ) from exc
        addresses = self._pinned_addresses.get(canonical_host)
        if addresses is None:
            # Every target and verification hostname is resolved before the run.
            # Falling back to the backend here would reintroduce a DNS-rebinding
            # window for IDNA-normalized or otherwise unexpected host spellings.
            raise httpcore.ConnectError(
                "HTTP transport requested a hostname that was not prevalidated"
            )

        failures: list[Exception] = []
        for address in addresses:
            try:
                stream = await self._backend.connect_tcp(
                    address,
                    port,
                    timeout=timeout,
                    local_address=local_address,
                    socket_options=socket_options,
                )
                return _wrap_request_fault_stream(stream)
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

    async def __aiter__(self) -> AsyncIterator[bytes]:
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
        pinned_addresses: dict[str, list[str]] = {}
        for target in targets:
            values = pinned_addresses.setdefault(target.hostname, [])
            for address in target.addresses:
                if address not in values:
                    values.append(address)
        self._pinned_addresses = {
            hostname: tuple(addresses)
            for hostname, addresses in pinned_addresses.items()
        }
        self._transport = self._build_transport()
        self._wire_transport = self._build_transport(
            limits=httpx.Limits(max_keepalive_connections=0)
        )

    def _build_transport(
        self,
        *,
        limits: httpx.Limits | None = None,
    ) -> httpx.AsyncHTTPTransport:
        transport = httpx.AsyncHTTPTransport(
            trust_env=False,
            limits=limits or httpx.Limits(),
        )
        # HTTPX delegates socket creation to its httpcore pool. Replace only
        # the network backend: the original URL host remains intact for the
        # HTTP Host header, TLS SNI, and certificate hostname verification.
        pool = getattr(transport, "_pool", None)
        backend = getattr(pool, "_network_backend", None)
        if pool is None or backend is None:
            raise RuntimeError(
                "The installed HTTPX transport does not expose the network "
                "backend needed to pin validated DNS results"
            )
        pool._network_backend = _PinnedNetworkBackend(
            backend,
            self._pinned_addresses,
        )
        return transport

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        fault = request.extensions.get("hookstorm_fault_injection")
        if fault == "connect_error":
            raise httpx.ConnectError(
                "injected connection failure before request reached the endpoint",
                request=request,
            )

        if fault in WIRE_REQUEST_FAULTS:
            body_length = int(request.extensions.get("hookstorm_request_body_length", 0))
            token = _REQUEST_FAULT_CONTEXT.set((fault, body_length))
            try:
                response = await self._wire_transport.handle_async_request(request)
            finally:
                _REQUEST_FAULT_CONTEXT.reset(token)
        else:
            response = await self._transport.handle_async_request(request)

        if fault == "response_loss":
            await response.aclose()
            raise httpx.ReadError(
                "injected acknowledgement loss after the endpoint processed the request",
                request=request,
            )
        if fault == "response_body_error":
            response.stream = _PartialResponseReadErrorStream(
                cast(httpx.AsyncByteStream, response.stream)
            )
        return response

    async def aclose(self) -> None:
        await self._transport.aclose()
        await self._wire_transport.aclose()


MAX_RESPONSE_BODY_BYTES = 2 * 1024 * 1024


async def _read_limited_response_body(response: httpx.Response) -> bytes | None:
    """Read an identity-encoded response body up to the memory-safety limit.

    Do not use HTTPX automatic decompression here: a small compressed chunk can
    expand into a huge allocation before the caller can enforce this limit. The
    runner requests identity encoding and rejects servers that ignore that request.
    """
    content_encoding = response.headers.get("content-encoding", "identity").strip().lower()
    if content_encoding not in {"", "identity"}:
        return None

    body = bytearray()
    async for chunk in response.aiter_raw():
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
                "Accept-Encoding": "identity",
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
                normalized_name = key.lower()
                if normalized_name not in reserved:
                    # HTTP field names are case-insensitive. Replace any
                    # built-in header with the same name instead of sending
                    # two differently-cased copies on the wire.
                    existing_name = next(
                        (
                            existing
                            for existing in base_headers
                            if existing.lower() == normalized_name
                        ),
                        None,
                    )
                    if existing_name is not None:
                        del base_headers[existing_name]
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
                    started_attempt = time.perf_counter()
                    fault_plan = delivery_config.get(
                        "fault_injection",
                        {
                            "connect_error_attempts": [],
                            "response_loss_attempts": [],
                            "response_body_error_attempts": [],
                        },
                    )
                    fault_attempt_types = (
                        ("connect_error_attempts", "connect_error"),
                        ("response_loss_attempts", "response_loss"),
                        ("response_body_error_attempts", "response_body_error"),
                        ("malformed_json_attempts", "malformed_json"),
                        ("wrong_content_type_attempts", "wrong_content_type"),
                        ("gzip_body_attempts", "gzip_body"),
                        ("deflate_body_attempts", "deflate_body"),
                        ("invalid_gzip_attempts", "invalid_gzip"),
                        ("invalid_deflate_attempts", "invalid_deflate"),
                        ("chunked_transfer_attempts", "chunked_transfer"),
                        ("malformed_chunked_attempts", "malformed_chunked"),
                        ("partial_upload_attempts", "partial_upload"),
                        ("content_length_mismatch_attempts", "content_length_mismatch"),
                    )
                    injected_fault = next(
                        (
                            fault_name
                            for field_name, fault_name in fault_attempt_types
                            if attempt in fault_plan.get(field_name, [])
                        ),
                        None,
                    )
                    headers = dict(base_headers)
                    request_body, request_fault_headers = prepare_request_body(
                        event_body,
                        injected_fault,
                    )
                    headers.update(request_fault_headers)
                    headers["X-HookStorm-Attempt"] = str(attempt)
                    headers["X-HookStorm-Duplicate-Index"] = str(duplicate_index)
                    headers.update(
                        _signature_headers(
                            request_body,
                            secret or "",
                            delivery_config["signature"],
                            delivery_config["signature_provider"],
                            event["event_id"],
                            delivery_config["signature_timestamp_offset_seconds"],
                        )
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
                            content=request_body,
                            headers=headers,
                            timeout=delivery_config["timeout_ms"] / 1000,
                            extensions=(
                                {
                                    "hookstorm_fault_injection": injected_fault,
                                    "hookstorm_request_body_length": len(request_body),
                                }
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
                                    "response body is compressed or exceeds the 2 MiB safety limit"
                                )
                            elif expected_json is not None:
                                try:
                                    matched, mismatch = _matches_subset(
                                        _parse_response_json(response_body), expected_json
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
                        elif injected_fault == "partial_upload":
                            outcome["error"] = "injected partial request upload"
                        elif injected_fault == "content_length_mismatch":
                            outcome["error"] = (
                                "injected Content-Length larger than the transmitted request body"
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
                                "Accept-Encoding": "identity",
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
                                    "verification response body is compressed or exceeds the 2 MiB safety limit",
                                )
                            else:
                                try:
                                    matched, mismatch = _matches_subset(
                                        _parse_response_json(response_body), verify_config["expect_json"]
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
