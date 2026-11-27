"""FastAPI adapter for a local HookStorm webhook test endpoint.

Install with: pip install "hookstorm[fastapi]"
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from hookstorm.integrations._recorder import WebhookRecorder, WebhookRequestError


@dataclass(frozen=True, slots=True)
class FastAPIWebhookEndpoint:
    """Router plus recorder, returned by :func:`create_router`."""

    router: Any
    recorder: WebhookRecorder


def create_router(
    *,
    path: str = "/__hookstorm/webhook",
    recorder: WebhookRecorder | None = None,
    max_body_bytes: int = 1_048_576,
    include_sensitive_headers: bool = False,
) -> FastAPIWebhookEndpoint:
    """Create an opt-in APIRouter that records synthetic JSON webhook requests."""
    if not path.startswith("/"):
        raise ValueError("path must be an absolute route path")

    try:
        from fastapi import APIRouter, HTTPException, Request
    except ImportError as exc:
        raise ImportError(
            'FastAPI integration requires the optional extra: pip install "hookstorm[fastapi]"'
        ) from exc

    active_recorder = recorder or WebhookRecorder(
        max_body_bytes=max_body_bytes,
        include_sensitive_headers=include_sensitive_headers,
    )
    router = APIRouter()

    async def receive_webhook(request: Any) -> dict[str, Any]:
        raw_body = await request.body()
        try:
            event = active_recorder.record(request.headers, raw_body)
        except WebhookRequestError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
        return event.response()

    # FastAPI must inspect the actual Request class. Resolve it at factory time
    # without making FastAPI a mandatory import for HookStorm's core package.
    receive_webhook.__annotations__["request"] = Request
    router.add_api_route(
        path,
        receive_webhook,
        methods=["POST"],
        name="hookstorm_test_webhook",
    )
    return FastAPIWebhookEndpoint(router=router, recorder=active_recorder)
