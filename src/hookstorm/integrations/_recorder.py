"""Framework-independent recorder used by optional webhook test endpoints."""

from __future__ import annotations

import collections.abc
import dataclasses
import datetime
import json
import threading
import typing
import zlib


_DEFAULT_MAX_BODY_BYTES = 1_048_576
_SENSITIVE_HEADERS = frozenset(
    {
        "authorization",
        "proxy-authorization",
        "cookie",
        "set-cookie",
        "x-hookstorm-signature",
        "stripe-signature",
        "x-shopify-hmac-sha256",
        "webhook-signature",
    }
)


class WebhookRequestError(ValueError):
    """A request rejected by the test recorder, with an HTTP response status."""

    def __init__(self, message: str, *, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclasses.dataclass(frozen=True, slots=True)
class WebhookRecord:
    """One captured request; raw_body contains the exact bytes received."""

    event_id: str | None
    run_id: str | None
    duplicate: bool
    payload: typing.Any
    raw_body: bytes
    headers: dict[str, str]
    received_at: str

    def response(self) -> dict[str, typing.Any]:
        """Return a compact JSON-safe acknowledgement for the sender."""
        return {
            "ok": True,
            "event_id": self.event_id,
            "run_id": self.run_id,
            "duplicate": self.duplicate,
        }


class WebhookRecorder:
    """Thread-safe, bounded recorder for synthetic JSON webhook requests.

    The recorder is intended for local development and isolated integration
    tests. It is not a production event store or a durable queue.
    """

    def __init__(
        self,
        *,
        max_body_bytes: int = _DEFAULT_MAX_BODY_BYTES,
        max_events: int = 1_000,
        include_sensitive_headers: bool = False,
    ) -> None:
        if (
            isinstance(max_body_bytes, bool)
            or not isinstance(max_body_bytes, int)
            or max_body_bytes < 1
        ):
            raise ValueError("max_body_bytes must be a positive integer")
        if (
            isinstance(max_events, bool)
            or not isinstance(max_events, int)
            or max_events < 1
        ):
            raise ValueError("max_events must be a positive integer")
        if not isinstance(include_sensitive_headers, bool):
            raise TypeError("include_sensitive_headers must be a boolean")
        self.max_body_bytes = max_body_bytes
        self.max_events = max_events
        self.include_sensitive_headers = include_sensitive_headers
        self._lock = threading.Lock()
        self._events: list[WebhookRecord] = []
        self._processed: set[tuple[str, str]] = set()

    @staticmethod
    def _header(headers: collections.abc.Mapping[str, typing.Any], *names: str) -> str | None:
        wanted = {name.lower() for name in names}
        for name, value in headers.items():
            if str(name).lower() in wanted:
                text = str(value).strip()
                return text or None
        return None

    @staticmethod
    def _reject_duplicate_keys(pairs: list[tuple[str, typing.Any]]) -> dict[str, typing.Any]:
        value: dict[str, typing.Any] = {}
        for key, item in pairs:
            if key in value:
                raise ValueError(f"duplicate JSON object key: {key}")
            value[key] = item
        return value

    def _decode_body(self, headers: collections.abc.Mapping[str, typing.Any], body: bytes) -> bytes:
        if len(body) > self.max_body_bytes:
            raise WebhookRequestError(
                f"request body exceeds the {self.max_body_bytes}-byte limit",
                status_code=413,
            )

        encoding = (self._header(headers, "Content-Encoding") or "identity").lower()
        if encoding in {"", "identity"}:
            return body
        if encoding == "gzip":
            window_bits = 16 + zlib.MAX_WBITS
        elif encoding == "deflate":
            window_bits = zlib.MAX_WBITS
        else:
            raise WebhookRequestError(
                f"unsupported Content-Encoding: {encoding}",
                status_code=415,
            )

        try:
            decoder = zlib.decompressobj(window_bits)
            decoded = decoder.decompress(body, self.max_body_bytes + 1)
            if len(decoded) > self.max_body_bytes or decoder.unconsumed_tail:
                raise WebhookRequestError(
                    f"decoded request body exceeds the {self.max_body_bytes}-byte limit",
                    status_code=413,
                )
            remaining = self.max_body_bytes + 1 - len(decoded)
            decoded += decoder.flush(remaining)
            if len(decoded) > self.max_body_bytes:
                raise WebhookRequestError(
                    f"decoded request body exceeds the {self.max_body_bytes}-byte limit",
                    status_code=413,
                )
            if not decoder.eof or decoder.unused_data:
                raise zlib.error("compressed body is truncated or has trailing data")
            return decoded
        except zlib.error as exc:
            raise WebhookRequestError(
                f"invalid {encoding} request body",
                status_code=400,
            ) from exc

    def _safe_headers(self, headers: collections.abc.Mapping[str, typing.Any]) -> dict[str, str]:
        result: dict[str, str] = {}
        for name, value in headers.items():
            key = str(name)
            normalized = key.lower()
            sensitive = (
                normalized in _SENSITIVE_HEADERS
                or normalized.startswith("x-hub-signature")
                or normalized.endswith("-signature")
            )
            result[key] = (
                "[REDACTED]"
                if sensitive and not self.include_sensitive_headers
                else str(value)
            )
        return result

    def record(self, headers: collections.abc.Mapping[str, typing.Any], raw_body: bytes) -> WebhookRecord:
        """Validate and retain one JSON request while preserving its wire bytes."""
        if not isinstance(raw_body, bytes):
            raise TypeError("raw_body must be bytes")
        content_type = (self._header(headers, "Content-Type") or "").split(";", 1)[0]
        content_type = content_type.strip().lower()
        if content_type != "application/json" and not (
            content_type.startswith("application/") and content_type.endswith("+json")
        ):
            raise WebhookRequestError(
                "Content-Type must be application/json or application/*+json",
                status_code=415,
            )

        decoded = self._decode_body(headers, raw_body)
        try:
            payload = json.loads(
                decoded.decode("utf-8"),
                object_pairs_hook=self._reject_duplicate_keys,
            )
        except (UnicodeDecodeError, ValueError) as exc:
            raise WebhookRequestError(
                "request body must contain valid UTF-8 JSON without duplicate object keys",
                status_code=400,
            ) from exc

        event_id = self._header(
            headers,
            "X-Event-ID",
            "webhook-id",
            "X-Shopify-Webhook-Id",
            "X-GitHub-Delivery",
        )
        run_id = self._header(headers, "X-HookStorm-Run")
        duplicate = False
        if event_id is not None:
            identity = (run_id or "", event_id)
            with self._lock:
                self._ensure_capacity()
                duplicate = identity in self._processed
                self._processed.add(identity)
                event = WebhookRecord(
                    event_id=event_id,
                    run_id=run_id,
                    duplicate=duplicate,
                    payload=payload,
                    raw_body=raw_body,
                    headers=self._safe_headers(headers),
                    received_at=datetime.datetime.now(datetime.UTC).isoformat(),
                )
                self._events.append(event)
                return event

        event = WebhookRecord(
            event_id=None,
            run_id=run_id,
            duplicate=False,
            payload=payload,
            raw_body=raw_body,
            headers=self._safe_headers(headers),
            received_at=datetime.datetime.now(datetime.UTC).isoformat(),
        )
        with self._lock:
            self._ensure_capacity()
            self._events.append(event)
        return event

    def _ensure_capacity(self) -> None:
        if len(self._events) >= self.max_events:
            raise WebhookRequestError(
                f"recorder reached its {self.max_events}-event limit; clear it before continuing",
                status_code=429,
            )

    @property
    def events(self) -> tuple[WebhookRecord, ...]:
        """Return a stable snapshot of captured records."""
        with self._lock:
            return tuple(self._events)

    def snapshot(self) -> dict[str, typing.Any]:
        """Return aggregate counters without exposing payloads or headers."""
        with self._lock:
            duplicates = sum(event.duplicate for event in self._events)
            return {
                "total_events": len(self._events),
                "unique_deliveries": len(self._processed),
                "duplicate_deliveries": duplicates,
                "event_ids": [event.event_id for event in self._events],
            }

    def clear(self) -> None:
        """Clear in-memory events and duplicate tracking between tests."""
        with self._lock:
            self._events.clear()
            self._processed.clear()
