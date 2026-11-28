"""Read-only integrations with external webhook provider APIs."""

from __future__ import annotations

import hashlib
import json
import os
import re
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
            return None, None
        if isinstance(decoded, dict):
            fingerprint, size = canonical_json_fingerprint(decoded)
            return fingerprint, size
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
        request_url = _safe_target(request.get("url"))
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
