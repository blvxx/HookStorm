"""Read-only integrations with external webhook provider APIs."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

_MAX_API_RESPONSE_BYTES = 2 * 1024 * 1024
_MAX_EVENT_PAYLOAD_BYTES = 1 * 1024 * 1024
_GITHUB_API_VERSION = "2022-11-28"
_COMPONENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$")


class ProviderSourceError(ValueError):
    """Raised when an external provider source cannot be read safely."""


def canonical_json_fingerprint(value: Any) -> tuple[str, int]:
    """Return a stable SHA-256 and byte count for a JSON value without exposing it."""
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (RecursionError, TypeError, ValueError, UnicodeEncodeError) as exc:
        raise ProviderSourceError("Provider payload is not valid bounded JSON") from exc
    if len(encoded) > _MAX_EVENT_PAYLOAD_BYTES:
        raise ProviderSourceError(
            f"Provider payload exceeds the {_MAX_EVENT_PAYLOAD_BYTES}-byte inspection limit"
        )
    return hashlib.sha256(encoded).hexdigest(), len(encoded)


def _safe_target(value: Any) -> str | None:
    """Return a credential- and query-free HTTP(S) target for comparison only."""
    if not isinstance(value, str) or len(value) > 4096:
        return None
    try:
        parsed = urlsplit(value)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
            return None
        if parsed.username or parsed.password:
            return None
        hostname = parsed.hostname.encode("idna").decode("ascii").lower()
        port = parsed.port
    except (UnicodeError, ValueError):
        return None
    netloc = f"[{hostname}]" if ":" in hostname else hostname
    if port is not None:
        netloc = f"{netloc}:{port}"
    path = parsed.path[:2048]
    return urlunsplit((parsed.scheme.lower(), netloc, path, "", ""))


def _safe_text(value: Any, maximum: int = 256) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or len(value) > maximum or any(ord(char) < 32 for char in value):
        return None
    return value


def _payload_fingerprint(value: Any) -> tuple[str | None, int | None]:
    if isinstance(value, dict):
        fingerprint, size = canonical_json_fingerprint(value)
        return fingerprint, size
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except (ValueError, TypeError):
            encoded = value.encode("utf-8")
            if len(encoded) <= _MAX_EVENT_PAYLOAD_BYTES:
                return hashlib.sha256(encoded).hexdigest(), len(encoded)
            return None, None
        if isinstance(decoded, dict):
            fingerprint, size = canonical_json_fingerprint(decoded)
            return fingerprint, size
        if isinstance(decoded, list):
            try:
                fingerprint, size = canonical_json_fingerprint(decoded)
            except ProviderSourceError:
                return None, None
            return fingerprint, size
        encoded = value.encode("utf-8")
        if len(encoded) <= _MAX_EVENT_PAYLOAD_BYTES:
            return hashlib.sha256(encoded).hexdigest(), len(encoded)
    return None, None


def _allowlisted_headers(value: Any, names: set[str]) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, str] = {}
    for key, item in value.items():
        if isinstance(key, str) and key.lower() in names:
            text = _safe_text(item, 512)
            if text is not None:
                result[key.lower()] = text
    return result


class GitHubWebhookAdapter:
    """Read repository webhook deliveries through GitHub's read-only REST API."""

    def __init__(
        self,
        token: str | None = None,
        *,
        client: httpx.Client | None = None,
    ) -> None:
        credential = token if token is not None else os.getenv("HOOKSTORM_GITHUB_TOKEN")
        if not isinstance(credential, str) or not credential.strip():
            raise ProviderSourceError(
                "GitHub delivery access is not configured; set HOOKSTORM_GITHUB_TOKEN "
                "to a token with repository Webhooks: read permission"
            )
        credential = credential.strip()
        if len(credential) > 4096 or any(ord(char) < 33 or ord(char) > 126 for char in credential):
            raise ProviderSourceError("HOOKSTORM_GITHUB_TOKEN contains invalid characters")

        self._token = credential
        self._owns_client = client is None
        self._client = client or httpx.Client(
            timeout=httpx.Timeout(8.0, connect=4.0),
            follow_redirects=False,
            trust_env=False,
        )

    def __enter__(self) -> GitHubWebhookAdapter:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    @staticmethod
    def _validate_location(owner: str, repo: str, hook_id: int) -> None:
        if not isinstance(owner, str) or not _COMPONENT_RE.fullmatch(owner):
            raise ProviderSourceError("owner must be a valid GitHub account or organization name")
        if not isinstance(repo, str) or not _COMPONENT_RE.fullmatch(repo):
            raise ProviderSourceError("repo must be a valid GitHub repository name")
        if isinstance(hook_id, bool) or not isinstance(hook_id, int) or hook_id < 1:
            raise ProviderSourceError("hook_id must be a positive integer")

    def _get_json(self, path: str, *, params: dict[str, Any] | None = None) -> Any:
        url = f"https://api.github.com{path}"
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self._token}",
            "X-GitHub-Api-Version": _GITHUB_API_VERSION,
            "User-Agent": "HookStorm-MCP",
        }
        try:
            with self._client.stream(
                "GET",
                url,
                params=params,
                headers=headers,
                follow_redirects=False,
            ) as response:
                body = bytearray()
                for chunk in response.iter_bytes():
                    if len(body) + len(chunk) > _MAX_API_RESPONSE_BYTES:
                        raise ProviderSourceError(
                            "GitHub API response exceeds the 2 MiB inspection limit"
                        )
                    body.extend(chunk)
                status_code = response.status_code
        except ProviderSourceError:
            raise
        except httpx.TimeoutException as exc:
            raise ProviderSourceError("GitHub API request timed out") from exc
        except httpx.HTTPError as exc:
            raise ProviderSourceError("GitHub API request failed before a response was received") from exc

        if not 200 <= status_code < 300:
            if status_code in {401, 403}:
                raise ProviderSourceError(
                    f"GitHub API denied access (HTTP {status_code}); verify the token, "
                    "repository access, and Webhooks: read permission"
                )
            if status_code == 404:
                raise ProviderSourceError(
                    "GitHub repository, webhook, or delivery was not found or is not accessible"
                )
            raise ProviderSourceError(f"GitHub API returned HTTP {status_code}")

        try:
            return json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProviderSourceError("GitHub API returned invalid JSON") from exc

    @staticmethod
    def _summary(item: Any) -> dict[str, Any]:
        if not isinstance(item, dict):
            raise ProviderSourceError("GitHub API returned a malformed delivery record")
        delivery_id = item.get("id")
        if isinstance(delivery_id, bool) or not isinstance(delivery_id, int):
            raise ProviderSourceError("GitHub API delivery record has no numeric ID")
        status_code = item.get("status_code")
        if isinstance(status_code, bool) or not isinstance(status_code, int):
            status_code = None
        duration = item.get("duration")
        if isinstance(duration, bool) or not isinstance(duration, (int, float)):
            duration = None
        return {
            "delivery_id": delivery_id,
            "guid": _safe_text(item.get("guid"), 128),
            "delivered_at": _safe_text(item.get("delivered_at"), 64),
            "redelivery": item.get("redelivery") if isinstance(item.get("redelivery"), bool) else None,
            "duration_seconds": duration,
            "status": _safe_text(item.get("status"), 64),
            "status_code": status_code,
            "event": _safe_text(item.get("event"), 128),
            "action": _safe_text(item.get("action"), 128),
            "repository_id": item.get("repository_id") if isinstance(item.get("repository_id"), int) else None,
            "installation_id": item.get("installation_id") if isinstance(item.get("installation_id"), int) else None,
            "throttled_at": _safe_text(item.get("throttled_at"), 64),
        }

    def list_deliveries(
        self,
        owner: str,
        repo: str,
        hook_id: int,
        *,
        limit: int = 20,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        self._validate_location(owner, repo, hook_id)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ProviderSourceError("limit must be an integer between 1 and 100")
        if status not in {None, "success", "failure"}:
            raise ProviderSourceError("status must be success, failure, or omitted")
        params: dict[str, Any] = {"per_page": limit}
        if status is not None:
            params["status"] = status
        payload = self._get_json(
            f"/repos/{owner}/{repo}/hooks/{hook_id}/deliveries",
            params=params,
        )
        if not isinstance(payload, list):
            raise ProviderSourceError("GitHub API returned an unexpected delivery list")
        if len(payload) > limit:
            raise ProviderSourceError("GitHub API returned more delivery records than requested")
        return [self._summary(item) for item in payload]

    def get_delivery(
        self,
        owner: str,
        repo: str,
        hook_id: int,
        delivery_id: int,
    ) -> dict[str, Any]:
        self._validate_location(owner, repo, hook_id)
        if (
            isinstance(delivery_id, bool)
            or not isinstance(delivery_id, int)
            or delivery_id < 1
        ):
            raise ProviderSourceError("delivery_id must be a positive integer")

        payload = self._get_json(
            f"/repos/{owner}/{repo}/hooks/{hook_id}/deliveries/{delivery_id}"
        )
        if not isinstance(payload, dict):
            raise ProviderSourceError("GitHub API returned a malformed delivery detail")

        request = payload.get("request")
        request = request if isinstance(request, dict) else {}
        response = payload.get("response")
        response = response if isinstance(response, dict) else {}
        request_headers = _allowlisted_headers(
            request.get("headers"),
            {"x-github-delivery", "x-github-event", "content-type"},
        )
        response_headers = _allowlisted_headers(
            response.get("headers"),
            {"content-type", "x-github-request-id"},
        )
        payload_hash, payload_size = _payload_fingerprint(request.get("payload"))
        response_hash, response_size = _payload_fingerprint(response.get("payload"))
        request_url = _safe_target(request.get("url") or payload.get("url"))
        status_code = payload.get("status_code")
        if isinstance(status_code, bool) or not isinstance(status_code, int):
            status_code = None
        response_status_code = response.get("status_code")
        if isinstance(response_status_code, bool) or not isinstance(response_status_code, int):
            response_status_code = None

        return {
            **self._summary(payload),
            "request": {
                "method": _safe_text(request.get("method"), 16),
                "target": request_url,
                "headers": request_headers,
                "payload_available": payload_hash is not None,
                "payload_sha256": payload_hash,
                "payload_size_bytes": payload_size,
            },
            "response": {
                "status_code": response_status_code,
                "headers": response_headers,
                "body_available": response_hash is not None,
                "body_sha256": response_hash,
                "body_size_bytes": response_size,
            },
            "privacy": (
                "Request payloads, arbitrary request/response headers, and response bodies are "
                "processed only to compute bounded metadata; their values are never returned."
            ),
        }



class StripeEventAdapter:
    """Retrieve one canonical Stripe Event; this is not a delivery-attempt log API."""

    def __init__(
        self,
        api_key: str | None = None,
        *,
        client: httpx.Client | None = None,
    ) -> None:
        credential = api_key if api_key is not None else os.getenv("HOOKSTORM_STRIPE_API_KEY")
        if not isinstance(credential, str) or not credential.strip():
            raise ProviderSourceError(
                "Stripe event access is not configured; set HOOKSTORM_STRIPE_API_KEY "
                "to a restricted or test-mode secret key with Events read access"
            )
        credential = credential.strip()
        if len(credential) > 4096 or any(ord(char) < 33 or ord(char) > 126 for char in credential):
            raise ProviderSourceError("HOOKSTORM_STRIPE_API_KEY contains invalid characters")

        self._api_key = credential
        self._owns_client = client is None
        self._client = client or httpx.Client(
            timeout=httpx.Timeout(8.0, connect=4.0),
            follow_redirects=False,
            trust_env=False,
        )

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> StripeEventAdapter:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def get_event(self, event_id: str) -> dict[str, Any]:
        if (
            not isinstance(event_id, str)
            or len(event_id) > 128
            or not re.fullmatch(r"evt_[A-Za-z0-9]+", event_id)
        ):
            raise ProviderSourceError("event_id must be a valid Stripe event identifier")

        try:
            with self._client.stream(
                "GET",
                f"https://api.stripe.com/v1/events/{event_id}",
                auth=(self._api_key, ""),
                headers={
                    "Accept": "application/json",
                    "User-Agent": "HookStorm-MCP",
                },
                follow_redirects=False,
            ) as response:
                body = bytearray()
                for chunk in response.iter_bytes():
                    if len(body) + len(chunk) > _MAX_API_RESPONSE_BYTES:
                        raise ProviderSourceError(
                            "Stripe API response exceeds the 2 MiB inspection limit"
                        )
                    body.extend(chunk)
                status_code = response.status_code
        except ProviderSourceError:
            raise
        except httpx.TimeoutException as exc:
            raise ProviderSourceError("Stripe API request timed out") from exc
        except httpx.HTTPError as exc:
            raise ProviderSourceError("Stripe API request failed before a response was received") from exc

        if not 200 <= status_code < 300:
            if status_code in {401, 403}:
                raise ProviderSourceError(
                    f"Stripe API denied access (HTTP {status_code}); verify the API key "
                    "and Events read permission"
                )
            if status_code == 404:
                raise ProviderSourceError(
                    "Stripe event was not found or is not accessible with this API key"
                )
            raise ProviderSourceError(f"Stripe API returned HTTP {status_code}")

        try:
            event = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProviderSourceError("Stripe API returned invalid JSON") from exc
        if not isinstance(event, dict) or event.get("id") != event_id:
            raise ProviderSourceError("Stripe API returned an unexpected event object")

        event_fingerprint, event_size = canonical_json_fingerprint(event)
        data = event.get("data")
        data_object = data.get("object") if isinstance(data, dict) else None
        if isinstance(data_object, dict):
            object_fingerprint, object_size = canonical_json_fingerprint(data_object)
        else:
            object_fingerprint, object_size = None, None

        created = event.get("created")
        if isinstance(created, bool) or not isinstance(created, int):
            created = None
        pending = event.get("pending_webhooks")
        if isinstance(pending, bool) or not isinstance(pending, int):
            pending = None
        livemode = event.get("livemode") if isinstance(event.get("livemode"), bool) else None
        request = event.get("request")
        request_id = request.get("id") if isinstance(request, dict) else None

        return {
            "provider": "stripe",
            "source": "stripe_events_api",
            "event_id": event_id,
            "event_type": _safe_text(event.get("type"), 256),
            "object": _safe_text(event.get("object"), 64),
            "created": created,
            "api_version": _safe_text(event.get("api_version"), 64),
            "livemode": livemode,
            "pending_webhooks": pending,
            "request_id": _safe_text(request_id, 128),
            "payload": {
                "sha256": event_fingerprint,
                "size_bytes": event_size,
            },
            "data_object": {
                "available": object_fingerprint is not None,
                "sha256": object_fingerprint,
                "size_bytes": object_size,
            },
            "delivery_attempt_history_available": False,
            "limitations": [
                "The Events API returns the canonical Event object, not a per-endpoint delivery-attempt history.",
                "Use the Stripe Dashboard webhook delivery view to inspect attempt status, response details, and retries.",
            ],
            "privacy": (
                "Event payload values and nested object fields are never returned; only canonical "
                "SHA-256 fingerprints, byte counts, and allowlisted event metadata are exposed."
            ),
        }


    @staticmethod
    def _summarize_event(event: Any) -> dict[str, Any]:
        if not isinstance(event, dict):
            raise ProviderSourceError("Stripe API returned a malformed Event")
        event_id = event.get("id")
        if (
            not isinstance(event_id, str)
            or not re.fullmatch(r"evt_[A-Za-z0-9]+", event_id)
        ):
            raise ProviderSourceError("Stripe API returned an Event without a valid ID")

        event_fingerprint, event_size = canonical_json_fingerprint(event)
        data = event.get("data")
        data_object = data.get("object") if isinstance(data, dict) else None
        if isinstance(data_object, dict):
            object_fingerprint, object_size = canonical_json_fingerprint(data_object)
        else:
            object_fingerprint, object_size = None, None

        created = event.get("created")
        if isinstance(created, bool) or not isinstance(created, int):
            created = None
        pending = event.get("pending_webhooks")
        if isinstance(pending, bool) or not isinstance(pending, int):
            pending = None
        livemode = event.get("livemode") if isinstance(event.get("livemode"), bool) else None
        request = event.get("request")
        request_id = request.get("id") if isinstance(request, dict) else None
        return {
            "provider": "stripe",
            "source": "stripe_events_api",
            "event_id": event_id,
            "event_type": _safe_text(event.get("type"), 256),
            "object": _safe_text(event.get("object"), 64),
            "created": created,
            "api_version": _safe_text(event.get("api_version"), 64),
            "livemode": livemode,
            "pending_webhooks": pending,
            "request_id": _safe_text(request_id, 128),
            "payload": {"sha256": event_fingerprint, "size_bytes": event_size},
            "data_object": {
                "available": object_fingerprint is not None,
                "sha256": object_fingerprint,
                "size_bytes": object_size,
            },
            "delivery_attempt_history_available": False,
        }

    def list_events(
        self,
        *,
        limit: int = 20,
        event_type: str | None = None,
        event_types: list[str] | None = None,
        delivery_success: bool | None = None,
        created_after: int | None = None,
        created_before: int | None = None,
        starting_after: str | None = None,
        ending_before: str | None = None,
    ) -> dict[str, Any]:
        """List recent Stripe Event objects; delivery_success is account-wide, not endpoint-specific."""
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ProviderSourceError("limit must be an integer between 1 and 100")
        if event_type is not None and (
            not isinstance(event_type, str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.*-]{0,127}", event_type)
        ):
            raise ProviderSourceError("event_type contains invalid characters")
        if event_types is not None:
            if (
                not isinstance(event_types, list)
                or not 1 <= len(event_types) <= 20
                or any(
                    not isinstance(item, str)
                    or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.*-]{0,127}", item)
                    for item in event_types
                )
                or len(set(event_types)) != len(event_types)
            ):
                raise ProviderSourceError("event_types must contain 1 to 20 unique valid event types")
        if event_type is not None and event_types is not None:
            raise ProviderSourceError("Use event_type or event_types, not both")
        if delivery_success is not None and not isinstance(delivery_success, bool):
            raise ProviderSourceError("delivery_success must be a boolean or omitted")
        for name, value in (("created_after", created_after), ("created_before", created_before)):
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value < 0
            ):
                raise ProviderSourceError(f"{name} must be a non-negative Unix timestamp")
        if created_after is not None and created_before is not None and created_after > created_before:
            raise ProviderSourceError("created_after must not be later than created_before")
        for name, value in (("starting_after", starting_after), ("ending_before", ending_before)):
            if value is not None and (
                not isinstance(value, str)
                or len(value) > 128
                or not re.fullmatch(r"evt_[A-Za-z0-9]+", value)
            ):
                raise ProviderSourceError(f"{name} must be a valid Stripe event ID")
        if starting_after is not None and ending_before is not None:
            raise ProviderSourceError("Use starting_after or ending_before, not both")

        params: list[tuple[str, str]] = [("limit", str(limit))]
        if event_type is not None:
            params.append(("type", event_type))
        if event_types is not None:
            params.extend(("types[]", item) for item in event_types)
        if delivery_success is not None:
            params.append(("delivery_success", str(delivery_success).lower()))
        if created_after is not None:
            params.append(("created[gte]", str(created_after)))
        if created_before is not None:
            params.append(("created[lte]", str(created_before)))
        if starting_after is not None:
            params.append(("starting_after", starting_after))
        if ending_before is not None:
            params.append(("ending_before", ending_before))

        try:
            with self._client.stream(
                "GET",
                "https://api.stripe.com/v1/events",
                params=params,
                auth=(self._api_key, ""),
                headers={"Accept": "application/json", "User-Agent": "HookStorm-MCP"},
                follow_redirects=False,
            ) as response:
                body = bytearray()
                for chunk in response.iter_bytes():
                    if len(body) + len(chunk) > _MAX_API_RESPONSE_BYTES:
                        raise ProviderSourceError(
                            "Stripe API response exceeds the 2 MiB inspection limit"
                        )
                    body.extend(chunk)
                status_code = response.status_code
        except ProviderSourceError:
            raise
        except httpx.TimeoutException as exc:
            raise ProviderSourceError("Stripe API request timed out") from exc
        except httpx.HTTPError as exc:
            raise ProviderSourceError("Stripe API request failed before a response was received") from exc

        if not 200 <= status_code < 300:
            if status_code in {401, 403}:
                raise ProviderSourceError(
                    f"Stripe API denied access (HTTP {status_code}); verify the API key "
                    "and Events read permission"
                )
            raise ProviderSourceError(f"Stripe API returned HTTP {status_code}")

        try:
            payload = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProviderSourceError("Stripe API returned invalid JSON") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            raise ProviderSourceError("Stripe API returned an unexpected Event list")
        if len(payload["data"]) > limit:
            raise ProviderSourceError("Stripe API returned more Events than requested")
        has_more = payload.get("has_more")
        if not isinstance(has_more, bool):
            raise ProviderSourceError("Stripe API returned an invalid pagination marker")
        events = [self._summarize_event(item) for item in payload["data"]]
        return {
            "provider": "stripe",
            "source": "stripe_events_api",
            "events": events,
            "count": len(events),
            "has_more": has_more,
            "next_cursor": events[-1]["event_id"] if has_more and events else None,
            "filters": {
                "event_type": event_type,
                "event_types": event_types,
                "delivery_success": delivery_success,
                "created_after": created_after,
                "created_before": created_before,
            },
            "delivery_success_semantics": (
                "Stripe applies this filter to whether all webhook endpoints have successfully "
                "received the Event. false also includes Events still pending delivery. It does "
                "not identify a particular endpoint or expose its individual delivery attempts."
                if delivery_success is not None
                else None
            ),
            "delivery_attempt_history_available": False,
            "limitations": [
                "Stripe Events API lists Event objects and supports an account-wide delivery_success filter.",
                "It does not return per-endpoint attempt logs, response bodies, or retry timelines.",
                "Inspect one endpoint's Webhook attempts in the Stripe Dashboard for those details.",
            ],
        }


class ShopifyAppLogsAdapter:
    """Read Shopify webhook log entries through the authenticated Shopify CLI 5.1+."""

    _MAX_LOOKBACK_HOURS = 168
    _MAX_WINDOW_HOURS = 24

    @classmethod
    def _duration_hours(cls, value: Any, name: str) -> int:
        if not isinstance(value, str) or len(value) > 4:
            raise ProviderSourceError(f"{name} must be a relative duration such as 24h or 2d")
        match = re.fullmatch(r"(0|[1-9][0-9]*)(h|d)", value)
        if match is None:
            raise ProviderSourceError(f"{name} must use whole hours or days, e.g. 24h or 2d")
        amount = int(match.group(1))
        hours = amount * (24 if match.group(2) == "d" else 1)
        if hours < 1 or hours > cls._MAX_LOOKBACK_HOURS:
            raise ProviderSourceError(f"{name} must be between 1 hour and 7 days")
        return hours

    def __init__(
        self,
        *,
        executable: str | None = None,
        runner: Any | None = None,
    ) -> None:
        configured = executable if executable is not None else os.getenv(
            "HOOKSTORM_SHOPIFY_CLI", "shopify"
        )
        if (
            not isinstance(configured, str)
            or not configured.strip()
            or len(configured) > 4096
            or any(ord(char) < 32 for char in configured)
        ):
            raise ProviderSourceError("HOOKSTORM_SHOPIFY_CLI must be a valid executable path")
        self._executable = configured.strip()
        self._runner = runner or subprocess.run

    @staticmethod
    def _first(record: dict[str, Any], names: tuple[str, ...]) -> Any:
        for name in names:
            if name in record:
                return record[name]
        return None

    @staticmethod
    def _records(value: Any) -> list[dict[str, Any]]:
        if isinstance(value, list):
            if not all(isinstance(item, dict) for item in value):
                raise ProviderSourceError("Shopify CLI returned a malformed log record")
            return value
        if isinstance(value, dict):
            for key in ("data", "logs", "records", "results"):
                nested = value.get(key)
                if isinstance(nested, list):
                    return ShopifyAppLogsAdapter._records(nested)
            if "id" in value or "timestamp" in value or "createdAt" in value:
                return [value]
        raise ProviderSourceError("Shopify CLI returned an unsupported JSON log format")

    @staticmethod
    def _sanitize_record(record: dict[str, Any]) -> dict[str, Any]:
        payload = ShopifyAppLogsAdapter._first(
            record,
            ("payload", "request_body", "requestBody", "request_payload", "requestPayload"),
        )
        payload_hash, payload_size = _payload_fingerprint(payload)
        target = _safe_target(
            ShopifyAppLogsAdapter._first(
                record,
                ("uri", "url", "destination", "target", "webhook_url", "webhookUrl"),
            )
        )
        status_code = ShopifyAppLogsAdapter._first(
            record,
            ("status_code", "statusCode", "response_status_code", "responseStatusCode", "response_code"),
        )
        if isinstance(status_code, bool) or not isinstance(status_code, int):
            status_code = None
        duration = ShopifyAppLogsAdapter._first(
            record,
            ("duration_ms", "durationMs", "response_time_ms", "responseTimeMs"),
        )
        if isinstance(duration, bool) or not isinstance(duration, (int, float)):
            duration = None
        attempt = ShopifyAppLogsAdapter._first(
            record,
            ("attempt", "attempt_number", "attemptNumber", "delivery_attempt"),
        )
        if isinstance(attempt, bool) or not isinstance(attempt, int):
            attempt = None
        return {
            "delivery_id": _safe_text(
                ShopifyAppLogsAdapter._first(
                    record,
                    ("id", "delivery_id", "deliveryId", "webhook_id", "webhookId"),
                ),
                128,
            ),
            "event_id": _safe_text(
                ShopifyAppLogsAdapter._first(
                    record,
                    ("event_id", "eventId", "webhook_id", "webhookId"),
                ),
                128,
            ),
            "timestamp": _safe_text(
                ShopifyAppLogsAdapter._first(
                    record,
                    ("timestamp", "created_at", "createdAt", "time"),
                ),
                64,
            ),
            "topic": _safe_text(
                ShopifyAppLogsAdapter._first(
                    record,
                    ("topic", "webhook_topic", "webhookTopic", "event_type", "eventType"),
                ),
                256,
            ),
            "status": _safe_text(
                ShopifyAppLogsAdapter._first(record, ("status", "result")),
                64,
            ),
            "status_code": status_code,
            "duration_ms": duration,
            "attempt": attempt,
            "shop": _safe_text(
                ShopifyAppLogsAdapter._first(record, ("shop", "shop_domain", "shopDomain", "store")),
                256,
            ),
            "target": target,
            "request_id": _safe_text(
                ShopifyAppLogsAdapter._first(record, ("request_id", "requestId")),
                128,
            ),
            "payload_available": payload_hash is not None,
            "payload_sha256": payload_hash,
            "payload_size_bytes": payload_size,
        }

    def list_webhook_deliveries(
        self,
        *,
        since: str = "1h",
        until: str | None = None,
        limit: int = 20,
        status: str | None = None,
        store: str | None = None,
        client_id: str | None = None,
        cwd: str | None = None,
        timeout_seconds: float = 20.0,
    ) -> list[dict[str, Any]]:
        import subprocess
        from pathlib import Path

        since_hours = self._duration_hours(since, "since")
        if until is None:
            if since_hours > self._MAX_WINDOW_HOURS:
                raise ProviderSourceError(
                    "since windows may be at most 24 hours; set until to query older periods"
                )
        else:
            until_hours = self._duration_hours(until, "until")
            if since_hours <= until_hours:
                raise ProviderSourceError("since must be earlier than until")
            if since_hours - until_hours > self._MAX_WINDOW_HOURS:
                raise ProviderSourceError("Shopify log query windows may not exceed 24 hours")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise ProviderSourceError("limit must be an integer between 1 and 1000")
        if status not in {None, "success", "failure"}:
            raise ProviderSourceError("status must be success, failure, or omitted")
        if store is not None and (
            not isinstance(store, str)
            or len(store) > 255
            or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9.-]*", store)
        ):
            raise ProviderSourceError("store must be a Shopify store domain without a URL scheme")
        if client_id is not None and (
            not isinstance(client_id, str)
            or len(client_id) > 128
            or not re.fullmatch(r"[A-Za-z0-9_-]+", client_id)
        ):
            raise ProviderSourceError("client_id contains invalid characters")
        if not isinstance(timeout_seconds, (int, float)) or isinstance(timeout_seconds, bool) or not 1 <= timeout_seconds <= 60:
            raise ProviderSourceError("timeout_seconds must be between 1 and 60")

        command = [
            self._executable,
            "app",
            "logs",
            "--type",
            "webhook",
            "--since",
            since,
        ]
        if until is not None:
            command.extend(["--until", until])
        command.extend(["--limit", str(limit), "--json", "--no-input"])
        if status is not None:
            command.extend(["--status", status])
        if store is not None:
            command.extend(["--store", store])
        if client_id is not None:
            command.extend(["--client-id", client_id])

        try:
            completed = self._runner(
                command,
                cwd=str(Path(cwd).resolve()) if cwd is not None else None,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout_seconds,
                check=False,
                shell=False,
                env={
                    **os.environ,
                    "SHOPIFY_CLI_NO_ANALYTICS": "1",
                    "OPT_OUT_INSTRUMENTATION": "true",
                },
            )
        except FileNotFoundError as exc:
            raise ProviderSourceError(
                "Shopify CLI was not found. Install Shopify CLI 5.1+ and set "
                "HOOKSTORM_SHOPIFY_CLI if its executable is not on PATH."
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise ProviderSourceError("Shopify CLI log query timed out") from exc
        except OSError as exc:
            raise ProviderSourceError("Shopify CLI could not be started") from exc

        stdout = completed.stdout if isinstance(completed.stdout, str) else ""
        if len(stdout.encode("utf-8", errors="replace")) > _MAX_API_RESPONSE_BYTES:
            raise ProviderSourceError(
                "Shopify CLI output exceeds the 2 MiB inspection limit; reduce --limit"
            )
        if completed.returncode != 0:
            raise ProviderSourceError(
                "Shopify CLI log query failed. Authenticate with shopify auth login, "
                "verify the linked app/store, and use Shopify CLI 5.1 or newer."
            )
        try:
            payload = json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise ProviderSourceError("Shopify CLI did not return valid JSON; ensure --json is supported") from exc

        records = self._records(payload)
        if len(records) > limit:
            raise ProviderSourceError("Shopify CLI returned more log records than requested")
        return [self._sanitize_record(record) for record in records]
