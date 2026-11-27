"""Loopback-only webhook capture and scenario export."""
from __future__ import annotations

import json
import os
import tempfile
import threading
from collections.abc import Mapping
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml

from hookstorm.config import ScenarioError, validate_scenario

MAX_CAPTURE_BODY_BYTES = 1_048_576
MAX_CAPTURE_SCENARIO_BYTES = 8 * 1_048_576
_SENSITIVE_HEADER_TOKENS = (
    "authorization",
    "authentication",
    "signature",
    "api-key",
    "api_key",
    "apikey",
    "auth-key",
    "auth_key",
    "auth-token",
    "auth_token",
    "access-key",
    "access_key",
    "access-token",
    "access_token",
    "client-key",
    "client_key",
    "private-key",
    "private_key",
    "encryption-key",
    "encryption_key",
    "token",
    "secret",
    "credential",
    "hmac",
    "cookie",
    "password",
)
_NEVER_CAPTURE_HEADERS = {
    "host", "content-length", "transfer-encoding", "connection", "accept-encoding",
    "keep-alive", "proxy-connection", "te", "trailer", "upgrade",
    "x-hookstorm-run", "x-hookstorm-attempt", "x-hookstorm-duplicate-index",
    "x-event-id", "x-event-type", "x-github-event", "x-github-delivery",
    "x-shopify-topic", "x-shopify-shop-domain", "x-shopify-api-version",
    "x-shopify-triggered-at", "x-shopify-webhook-id", "x-shopify-event-id",
    "webhook-id", "webhook-timestamp",
}


class CaptureError(ValueError):
    """A capture rejection with the HTTP status the local receiver should return."""

    def __init__(self, message: str, *, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Build a JSON object while rejecting ambiguous duplicate member names."""
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise CaptureError(f"JSON object contains duplicate key: {key!r}")
        result[key] = value
    return result


class CaptureStore:
    """Collect JSON webhook requests and atomically persist a version 1 scenario."""

    def __init__(
        self,
        output: str | Path,
        *,
        include_sensitive_headers: bool = False,
        max_events: int = 25,
    ) -> None:
        if (
            isinstance(max_events, bool)
            or not isinstance(max_events, int)
            or not 1 <= max_events <= 500
        ):
            raise CaptureError("max_events must be between 1 and 500")
        self.output = Path(output)
        self.include_sensitive_headers = include_sensitive_headers
        self.max_events = max_events
        self.events: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        self.capture_events: list[dict[str, Any]] = []
        self.total_body_bytes = 0

    @staticmethod
    def _is_sensitive(name: str) -> bool:
        lowered = name.lower()
        return any(token in lowered for token in _SENSITIVE_HEADER_TOKENS)

    @staticmethod
    def _event_type(headers: Mapping[str, str], payload: Any) -> str:
        lowered = {key.lower(): value for key, value in headers.items()}
        github_event = lowered.get("x-github-event")
        if github_event:
            action = payload.get("action") if isinstance(payload, dict) else None
            return (
                f"{github_event}.{action}"
                if isinstance(action, str) and action
                else github_event
            )
        for key in ("x-shopify-topic", "x-event-type"):
            value = lowered.get(key)
            if value:
                return value
        if isinstance(payload, dict) and isinstance(payload.get("type"), str):
            return payload["type"]
        return "captured.webhook"

    @staticmethod
    def _original_event_id(headers: Mapping[str, str]) -> str | None:
        lowered = {key.lower(): value for key, value in headers.items()}
        for name in (
            "x-event-id",
            "webhook-id",
            "x-github-delivery",
            "x-shopify-webhook-id",
        ):
            if name in lowered:
                return lowered[name]
        return None

    def record(
        self,
        headers: Mapping[str, str],
        body: bytes,
        *,
        request_path: str = "/",
    ) -> dict[str, Any]:
        """Serialize concurrent captures so IDs and atomic writes cannot race."""
        with self._lock:
            return self._record_locked(headers, body, request_path=request_path)

    def _record_locked(
        self,
        headers: Mapping[str, str],
        body: bytes,
        *,
        request_path: str,
    ) -> dict[str, Any]:
        """Validate and append one JSON delivery, then atomically persist it."""
        if len(body) > MAX_CAPTURE_BODY_BYTES:
            raise CaptureError("request body exceeds the 1 MiB capture limit", status_code=413)
        if len(self.events) >= self.max_events:
            raise CaptureError(
                f"capture reached its {self.max_events}-event limit", status_code=429
            )
        if self.total_body_bytes + len(body) > MAX_CAPTURE_SCENARIO_BYTES:
            raise CaptureError(
                "captured scenario would exceed the 8 MiB total body limit",
                status_code=413,
            )
        try:
            payload = json.loads(
                body.decode("utf-8"),
                object_pairs_hook=_unique_json_object,
            )
        except CaptureError:
            raise
        except (UnicodeDecodeError, ValueError, RecursionError) as exc:
            raise CaptureError(
                "capture accepts valid UTF-8 JSON request bodies within parser limits"
            ) from exc

        normalized_headers: dict[str, str] = {}
        sensitive_headers: dict[str, str] = {}
        for name, value in headers.items():
            lowered = name.lower()
            if lowered in _NEVER_CAPTURE_HEADERS:
                continue
            if self._is_sensitive(name):
                if self.include_sensitive_headers:
                    sensitive_headers[name] = value
                continue
            if len(normalized_headers) < 64:
                normalized_headers[name] = value

        event_id = f"evt_capture_{len(self.events) + 1:04d}"
        event = {
            "id": self._event_type(headers, payload),
            "event_id": event_id,
            "payload": payload,
            "headers": normalized_headers,
        }
        self.events.append(event)
        self.capture_events.append(
            {
                "event_id": event_id,
                "path": urlsplit(request_path).path or "/",
                "original_event_id": self._original_event_id(headers),
                "sensitive_headers": sensitive_headers,
            }
        )
        self.total_body_bytes += len(body)

        candidate = {
            "version": 1,
            "name": "Captured webhook scenario",
            "description": (
                "Captured locally by HookStorm. Review payloads and headers before sharing."
            ),
            "delivery": {},
            "events": self.events,
            "capture": {
                "source": "hookstorm.capture",
                "events": self.capture_events,
            },
        }
        try:
            validate_scenario(candidate)
            self._write(candidate)
        except Exception as exc:
            self.events.pop()
            self.capture_events.pop()
            self.total_body_bytes -= len(body)
            if isinstance(exc, ScenarioError):
                raise CaptureError(
                    f"captured request cannot be represented as a HookStorm scenario: {exc}"
                ) from exc
            if isinstance(exc, RecursionError):
                raise CaptureError(
                    "captured request exceeds the supported JSON/YAML nesting limits"
                ) from exc
            raise
        return event

    def _write(self, scenario: dict[str, Any]) -> None:
        """Persist with restrictive permissions and an atomic replace."""
        self.output.parent.mkdir(parents=True, exist_ok=True)
        serialized = yaml.safe_dump(
            scenario, allow_unicode=True, sort_keys=False, default_flow_style=False
        )
        if len(serialized.encode("utf-8")) > MAX_CAPTURE_SCENARIO_BYTES:
            raise CaptureError(
                "captured scenario exceeds the 8 MiB file limit", status_code=413
            )
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{self.output.name}.", suffix=".tmp", dir=self.output.parent
        )
        try:
            if os.name == "posix":
                fchmod = getattr(os, "fchmod", None)
                if callable(fchmod):
                    fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
                stream.write(serialized)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_name, self.output)
            if os.name == "posix":
                os.chmod(self.output, 0o600)
        except Exception:
            try:
                os.close(descriptor)
            except OSError:
                pass
            try:
                os.unlink(temporary_name)
            except OSError:
                pass
            raise


class _CaptureServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def create_capture_server(
    output: str | Path,
    *,
    port: int = 8787,
    include_sensitive_headers: bool = False,
    max_events: int = 25,
) -> tuple[ThreadingHTTPServer, CaptureStore]:
    """Create an HTTP capture server bound strictly to IPv4 loopback."""
    if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65535:
        raise CaptureError("port must be between 0 and 65535")
    store = CaptureStore(
        output,
        include_sensitive_headers=include_sensitive_headers,
        max_events=max_events,
    )

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def setup(self) -> None:
            super().setup()
            self.connection.settimeout(5.0)

        def log_message(self, fmt: str, *args: object) -> None:
            return

        def _respond(self, status: int, message: str) -> None:
            body = json.dumps({"message": message}, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)
            self.close_connection = True

        def do_POST(self) -> None:  # noqa: N802
            if self.headers.get("Transfer-Encoding") is not None:
                self._respond(501, "chunked request bodies are not supported by capture")
                return
            lengths = self.headers.get_all("Content-Length", [])
            if len(lengths) != 1 or not lengths[0].isdigit():
                self._respond(411, "one valid Content-Length header is required")
                return
            length = int(lengths[0])
            if length > MAX_CAPTURE_BODY_BYTES:
                self._respond(413, "request body exceeds the 1 MiB capture limit")
                return
            try:
                body = self.rfile.read(length)
            except OSError:
                self._respond(408, "request body could not be read before the connection timed out")
                return
            if len(body) != length:
                self._respond(
                    400,
                    "request body ended before Content-Length bytes were received",
                )
                return
            try:
                event = store.record(
                    dict(self.headers.items()),
                    body,
                    request_path=self.path,
                )
            except CaptureError as exc:
                self._respond(exc.status_code, str(exc))
                return
            except OSError:
                self._respond(500, "capture could not be persisted")
                return
            self._respond(202, f"captured {event['event_id']}")

        def do_GET(self) -> None:  # noqa: N802
            self._respond(
                200,
                f"HookStorm capture ready; {len(store.events)} event(s) captured",
            )

        def do_PUT(self) -> None:  # noqa: N802
            self._respond(405, "only POST is accepted")

        def do_DELETE(self) -> None:  # noqa: N802
            self._respond(405, "only POST is accepted")

    server = _CaptureServer(("127.0.0.1", port), Handler)
    server.timeout = 1.0
    return server, store
