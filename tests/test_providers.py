from __future__ import annotations

import json

import httpx
import pytest

from hookstorm.providers import (
    GitHubWebhookAdapter,
    ProviderSourceError,
    ShopifyAppLogsAdapter,
    StripeEventAdapter,
    canonical_json_fingerprint,
)

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
        "url": "https://receiver.example.test/webhook?token=url-secret",
        "request": {
            "method": "POST",
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


def test_stripe_adapter_retrieves_event_metadata_and_never_returns_event_payload() -> None:
    event = {
        "id": "evt_1A2B3C",
        "object": "event",
        "type": "payment_intent.succeeded",
        "created": 1795860000,
        "api_version": "2025-09-30.clover",
        "livemode": False,
        "pending_webhooks": 2,
        "request": {"id": "req_123"},
        "data": {
            "object": {
                "id": "pi_secret",
                "object": "payment_intent",
                "metadata": {"private": "customer-secret"},
            }
        },
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.host == "api.stripe.com"
        assert request.url.path == "/v1/events/evt_1A2B3C"
        assert request.headers["Authorization"].startswith("Basic ")
        return httpx.Response(200, json=event)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with StripeEventAdapter("sk_test_stripe-secret", client=client) as adapter:
        result = adapter.get_event("evt_1A2B3C")
    client.close()

    expected_hash, expected_size = canonical_json_fingerprint(event)
    assert result["event_id"] == event["id"]
    assert result["event_type"] == "payment_intent.succeeded"
    assert result["pending_webhooks"] == 2
    assert result["payload"] == {"sha256": expected_hash, "size_bytes": expected_size}
    assert result["delivery_attempt_history_available"] is False
    serialized = json.dumps(result)
    for secret in ("stripe-secret", "pi_secret", "customer-secret"):
        assert secret not in serialized


def test_stripe_adapter_rejects_invalid_ids_and_redirects() -> None:
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                302,
                headers={"Location": "https://attacker.example.test/collect"},
            )
        ),
        follow_redirects=True,
    )
    with StripeEventAdapter("sk_test_token", client=client) as adapter:
        with pytest.raises(ProviderSourceError, match="event identifier"):
            adapter.get_event("evt_../secret")
        with pytest.raises(ProviderSourceError, match="HTTP 302"):
            adapter.get_event("evt_1A2B3C")
    client.close()


def test_shopify_adapter_runs_read_only_bounded_json_command_and_sanitizes() -> None:
    from types import SimpleNamespace

    calls: list[tuple[list[str], dict]] = []
    payload = [
        {
            "id": "delivery-1",
            "eventId": "shp_event_1",
            "createdAt": "2026-11-28T10:00:00Z",
            "topic": "orders/create",
            "status": "failure",
            "statusCode": 503,
            "durationMs": 250,
            "attempt": 2,
            "shopDomain": "dev-store.myshopify.com",
            "uri": "https://receiver.example.test/webhook?token=private-url",
            "payload": {"order_id": 42, "secret": "payload-secret"},
            "responseBody": "response-secret",
            "authorization": "authorization-secret",
        }
    ]

    def fake_runner(command: list[str], **kwargs: object) -> SimpleNamespace:
        calls.append((command, kwargs))
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(payload),
            stderr="private stderr that must not appear in errors",
        )

    adapter = ShopifyAppLogsAdapter(executable="/usr/local/bin/shopify", runner=fake_runner)
    result = adapter.list_webhook_deliveries(
        since="24h",
        limit=100,
        status="failure",
        store="dev-store.myshopify.com",
        client_id="client_123",
        cwd="/tmp/shopify-app",
    )

    command, kwargs = calls[0]
    assert command[:8] == [
        "/usr/local/bin/shopify",
        "app",
        "logs",
        "--type",
        "webhook",
        "--since",
        "24h",
        "--limit",
    ]
    assert "--json" in command and "--no-input" in command
    assert kwargs["shell"] is False
    assert kwargs["capture_output"] is True
    assert kwargs["timeout"] == 20.0
    assert kwargs["env"]["SHOPIFY_CLI_NO_ANALYTICS"] == "1"
    assert kwargs["env"]["OPT_OUT_INSTRUMENTATION"] == "true"
    assert result[0]["delivery_id"] == "delivery-1"
    assert result[0]["event_id"] == "shp_event_1"
    assert result[0]["status_code"] == 503
    assert result[0]["attempt"] == 2
    assert result[0]["target"] == "https://receiver.example.test/webhook"
    expected_hash, expected_size = canonical_json_fingerprint(payload[0]["payload"])
    assert result[0]["payload_sha256"] == expected_hash
    assert result[0]["payload_size_bytes"] == expected_size
    serialized = json.dumps(result)
    for secret in ("private-url", "payload-secret", "response-secret", "authorization-secret"):
        assert secret not in serialized


def test_shopify_adapter_rejects_option_injection_and_invalid_ranges() -> None:
    adapter = ShopifyAppLogsAdapter(runner=lambda command, **kwargs: None)
    with pytest.raises(ProviderSourceError, match="store"):
        adapter.list_webhook_deliveries(store="shop.myshopify.com;whoami")
    with pytest.raises(ProviderSourceError, match="since"):
        adapter.list_webhook_deliveries(since="30d")
    with pytest.raises(ProviderSourceError, match="limit"):
        adapter.list_webhook_deliveries(limit=1001)


def test_shopify_adapter_hides_cli_errors_and_bounds_output() -> None:
    import subprocess
    from types import SimpleNamespace

    def failed_runner(command: list[str], **kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(
            returncode=1,
            stdout="",
            stderr="access token secret and private details",
        )

    adapter = ShopifyAppLogsAdapter(runner=failed_runner)
    with pytest.raises(ProviderSourceError) as error:
        adapter.list_webhook_deliveries()
    assert "access token secret" not in str(error.value)

    def huge_runner(command: list[str], **kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(
            returncode=0,
            stdout=" " * (2 * 1024 * 1024 + 1),
            stderr="",
        )

    with pytest.raises(ProviderSourceError, match="2 MiB"):
        ShopifyAppLogsAdapter(runner=huge_runner).list_webhook_deliveries()

    def timeout_runner(command: list[str], **kwargs: object) -> SimpleNamespace:
        raise subprocess.TimeoutExpired(command, timeout=20)

    with pytest.raises(ProviderSourceError, match="timed out"):
        ShopifyAppLogsAdapter(runner=timeout_runner).list_webhook_deliveries()
