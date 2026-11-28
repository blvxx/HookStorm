from __future__ import annotations

import json

import httpx
import pytest

from hookstorm.providers import GitHubWebhookAdapter, ProviderSourceError, canonical_json_fingerprint


GUID = "0b989ba4-242f-11e5-81e1-c7b6966d2516"


def _detail() -> dict:
    return {
        "id": 12345678,
        "guid": GUID,
        "delivered_at": "2026-11-28T10:00:00Z",
        "redelivery": False,
        "duration": 0.27,
        "status": "OK",
        "status_code": 503,
        "event": "issues",
        "action": "opened",
        "repository_id": 456,
        "installation_id": 123,
        "request": {
            "method": "POST",
            "url": "https://receiver.example.test/webhook?token=url-secret",
            "headers": {
                "X-GitHub-Delivery": GUID,
                "X-GitHub-Event": "issues",
                "Content-Type": "application/json",
                "Authorization": "header-secret",
                "X-Hub-Signature-256": "signature-secret",
            },
            "payload": {
                "action": "opened",
                "issue": {"number": 42, "private_value": "payload-secret"},
            },
        },
        "response": {
            "status_code": 503,
            "headers": {
                "Content-Type": "application/json",
                "X-GitHub-Request-Id": "request-id",
                "Set-Cookie": "cookie-secret",
            },
            "payload": {
                "error": "database unavailable",
                "private_value": "response-secret",
            },
        },
    }


def test_github_adapter_lists_only_allowlisted_delivery_metadata() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        assert request.method == "GET"
        assert request.headers["Authorization"] == "Bearer github-token-secret"
        assert request.headers["X-GitHub-Api-Version"] == "2022-11-28"
        assert request.url.params["per_page"] == "10"
        assert request.url.params["status"] == "failure"
        return httpx.Response(
            200,
            json=[
                {
                    "id": 7,
                    "guid": GUID,
                    "delivered_at": "2026-11-28T10:00:00Z",
                    "redelivery": True,
                    "duration": 0.2,
                    "status": "OK",
                    "status_code": 200,
                    "event": "issues",
                    "action": "opened",
                    "secret_field": "must-not-leak",
                }
            ],
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with GitHubWebhookAdapter(
        "github-token-secret",
        client=client,
    ) as adapter:
        result = adapter.list_deliveries(
            "octocat",
            "hello-world",
            42,
            limit=10,
            status="failure",
        )

    assert len(seen) == 1
    assert result == [
        {
            "delivery_id": 7,
            "guid": GUID,
            "delivered_at": "2026-11-28T10:00:00Z",
            "redelivery": True,
            "duration_seconds": 0.2,
            "status": "OK",
            "status_code": 200,
            "event": "issues",
            "action": "opened",
            "repository_id": None,
            "installation_id": None,
            "throttled_at": None,
        }
    ]
    assert "must-not-leak" not in json.dumps(result)
    client.close()


def test_github_adapter_hashes_bodies_and_redacts_unapproved_headers() -> None:
    detail = _detail()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.host == "api.github.com"
        return httpx.Response(200, json=detail)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with GitHubWebhookAdapter("github-token-secret", client=client) as adapter:
        result = adapter.get_delivery("octocat", "hello-world", 42, 12345678)
    client.close()

    expected_hash, expected_size = canonical_json_fingerprint(detail["request"]["payload"])
    assert result["guid"] == GUID
    assert result["status_code"] == 503
    assert result["request"]["target"] == "https://receiver.example.test/webhook"
    assert result["request"]["headers"] == {
        "x-github-delivery": GUID,
        "x-github-event": "issues",
        "content-type": "application/json",
    }
    assert result["request"]["payload_sha256"] == expected_hash
    assert result["request"]["payload_size_bytes"] == expected_size
    assert result["response"]["status_code"] == 503
    assert result["response"]["body_available"] is True

    serialized = json.dumps(result)
    for secret in (
        "github-token-secret",
        "url-secret",
        "header-secret",
        "signature-secret",
        "payload-secret",
        "response-secret",
        "cookie-secret",
        "database unavailable",
    ):
        assert secret not in serialized


def test_github_adapter_requires_explicit_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HOOKSTORM_GITHUB_TOKEN", raising=False)
    with pytest.raises(ProviderSourceError, match="HOOKSTORM_GITHUB_TOKEN"):
        GitHubWebhookAdapter()


@pytest.mark.parametrize(
    ("owner", "repo", "hook_id", "limit", "status"),
    [
        ("owner/other", "repo", 1, 10, None),
        ("owner", "../repo", 1, 10, None),
        ("owner", "repo", 0, 10, None),
        ("owner", "repo", True, 10, None),
        ("owner", "repo", 1, 101, None),
        ("owner", "repo", 1, 10, "pending"),
    ],
)
def test_github_adapter_validates_api_parameters(
    owner: str,
    repo: str,
    hook_id: int,
    limit: int,
    status: str | None,
) -> None:
    client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(500)))
    with GitHubWebhookAdapter("token", client=client) as adapter:
        with pytest.raises(ProviderSourceError):
            adapter.list_deliveries(owner, repo, hook_id, limit=limit, status=status)
    client.close()


def test_github_adapter_rejects_oversized_api_responses() -> None:
    response_body = b"[" + (b" " * (2 * 1024 * 1024))

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=response_body)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with GitHubWebhookAdapter("token", client=client) as adapter:
        with pytest.raises(ProviderSourceError, match="2 MiB"):
            adapter.list_deliveries("octocat", "hello-world", 42)
    client.close()


def test_github_adapter_does_not_follow_api_redirects() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(302, headers={"Location": "https://attacker.example.test/collect"})

    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
    with GitHubWebhookAdapter("token", client=client) as adapter:
        with pytest.raises(ProviderSourceError, match="HTTP 302"):
            adapter.list_deliveries("octocat", "hello-world", 42)
    assert len(seen) == 1
    assert seen[0].url.host == "api.github.com"
    client.close()
