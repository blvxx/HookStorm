"""Flask adapter for a local HookStorm webhook test endpoint.

Install with: pip install "hookstorm[flask]"
"""

from __future__ import annotations

from typing import Any

from hookstorm.integrations._recorder import WebhookRecorder, WebhookRequestError


def register_endpoint(
    app: Any,
    *,
    path: str = "/__hookstorm/webhook",
    endpoint: str = "hookstorm_test_webhook",
    recorder: WebhookRecorder | None = None,
    max_body_bytes: int = 1_048_576,
    max_events: int = 1_000,
    max_total_body_bytes: int = 8 * 1_048_576,
    include_sensitive_headers: bool = False,
) -> WebhookRecorder:
    """Register an opt-in Flask POST route and return its in-memory recorder."""
    if not path.startswith("/"):
        raise ValueError("path must be an absolute route path")
    if not endpoint.strip():
        raise ValueError("endpoint must not be empty")

    try:
        from flask import jsonify, request
    except ImportError as exc:
        raise ImportError(
            'Flask integration requires the optional extra: pip install "hookstorm[flask]"'
        ) from exc

    active_recorder = recorder or WebhookRecorder(
        max_body_bytes=max_body_bytes,
        max_events=max_events,
        max_total_body_bytes=max_total_body_bytes,
        include_sensitive_headers=include_sensitive_headers,
    )

    def receive_webhook():
        try:
            raw_body = request.stream.read(active_recorder.max_body_bytes + 1)
            if len(raw_body) > active_recorder.max_body_bytes:
                raise WebhookRequestError(
                    f"request body exceeds the {active_recorder.max_body_bytes}-byte limit",
                    status_code=413,
                )
            event = active_recorder.record(request.headers, raw_body)
        except WebhookRequestError as exc:
            return jsonify({"ok": False, "error": str(exc)}), exc.status_code
        return jsonify(event.response()), 200

    app.add_url_rule(path, endpoint, receive_webhook, methods=["POST"])
    return active_recorder
