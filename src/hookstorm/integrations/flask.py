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
        include_sensitive_headers=include_sensitive_headers,
    )

    def receive_webhook():
        raw_body = request.get_data(cache=False, as_text=False)
        try:
            event = active_recorder.record(request.headers, raw_body)
        except WebhookRequestError as exc:
            return jsonify({"ok": False, "error": str(exc)}), exc.status_code
        return jsonify(event.response()), 200

    app.add_url_rule(path, endpoint, receive_webhook, methods=["POST"])
    return active_recorder
