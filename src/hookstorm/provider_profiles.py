"""Provider event profiles and provider-managed request headers."""

from __future__ import annotations

import copy
import uuid
from typing import Any

PROVIDER_NAMES = ("stripe", "github", "shopify", "standard_webhooks")
_DELIVERY_NAMESPACE = uuid.UUID("6b7fcf82-0a1e-4ae5-91a9-f5e7d0e10a4f")

_PROFILE_TEMPLATES: dict[str, dict[str, Any]] = {
    "stripe": {
        "event_type": "payment_intent.succeeded",
        "event_id_prefix": "evt_hookstorm_stripe",
        "payload": {
            "object": "event",
            "type": "payment_intent.succeeded",
            "livemode": False,
            "data": {
                "object": {
                    "id": "pi_hookstorm_001",
                    "object": "payment_intent",
                    "amount": 1250,
                    "currency": "usd",
                    "status": "succeeded",
                }
            },
        },
    },
    "github": {
        "event_type": "issues.opened",
        "event_id_prefix": "evt_hookstorm_github",
        "payload": {
            "action": "opened",
            "issue": {
                "number": 42,
                "title": "Example issue from HookStorm",
                "state": "open",
                "user": {"login": "hookstorm-test"},
            },
            "repository": {"full_name": "example-org/example-repo"},
        },
    },
    "shopify": {
        "event_type": "orders/create",
        "event_id_prefix": "evt_hookstorm_shopify",
        "payload": {
            "id": 1000001,
            "currency": "USD",
            "total_price": "12.50",
            "financial_status": "paid",
            "line_items": [
                {"id": 2000001, "title": "Example item", "quantity": 1, "price": "12.50"}
            ],
        },
    },
    "standard_webhooks": {
        "event_type": "order.created",
        "event_id_prefix": "evt_hookstorm_standard",
        "payload": {
            "type": "order.created",
            "data": {"object": {"id": "ord_hookstorm_001", "status": "created"}},
        },
    },
}


def provider_event(
    provider: str,
    index: int = 0,
    *,
    event_type: str | None = None,
    event_id: str | None = None,
) -> dict[str, Any]:
    """Return a fresh synthetic event template for one provider profile."""
    if provider not in _PROFILE_TEMPLATES:
        raise ValueError(f"Unsupported provider profile: {provider}")
    if isinstance(index, bool) or not isinstance(index, int) or index < 0:
        raise ValueError("Provider profile event index must be a non-negative integer")

    template = _PROFILE_TEMPLATES[provider]
    resolved_event_id = event_id or f"{template['event_id_prefix']}_{index + 1:03d}"
    resolved_event_type = event_type or template["event_type"]
    payload = copy.deepcopy(template["payload"])
    if provider == "stripe":
        payload["id"] = resolved_event_id
        payload["type"] = resolved_event_type
    elif provider == "standard_webhooks":
        payload["type"] = resolved_event_type

    return {
        "id": resolved_event_type,
        "event_id": resolved_event_id,
        "payload": payload,
    }


def provider_headers(provider: str, event_type: str, event_id: str) -> dict[str, str]:
    """Build non-signature provider headers for a synthetic event."""
    if provider == "github":
        delivery_id = uuid.uuid5(_DELIVERY_NAMESPACE, f"github:{event_id}")
        return {
            "X-GitHub-Event": event_type.split(".", 1)[0],
            "X-GitHub-Delivery": str(delivery_id),
        }
    if provider == "shopify":
        delivery_id = uuid.uuid5(_DELIVERY_NAMESPACE, f"shopify:{event_id}")
        return {
            "X-Shopify-Topic": event_type,
            "X-Shopify-Shop-Domain": "hookstorm-test.myshopify.com",
            "X-Shopify-Webhook-Id": str(delivery_id),
        }
    if provider in {"stripe", "standard_webhooks"}:
        return {}
    raise ValueError(f"Unsupported provider profile: {provider}")


def provider_event_defaults(provider: str) -> dict[str, Any]:
    """Return event and request-header defaults for documentation and scaffolding."""
    event = provider_event(provider)
    return {
        **event,
        "headers": provider_headers(provider, event["id"], event["event_id"]),
    }
