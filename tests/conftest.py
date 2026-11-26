from __future__ import annotations

import base64
import hashlib
import hmac
import json
import socket
import threading
import time
from collections import defaultdict
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest


@pytest.fixture
def receiver() -> Iterator[tuple[str, dict]]:
    state = {
        "seen": set(),
        "effects": set(),
        "attempts": defaultdict(int),
        "requests": [],
        "verification_requests": [],
        "active": 0,
        "max_active": 0,
    }
    state_lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: object) -> None:
            return

        def do_GET(self) -> None:  # noqa: N802
            if self.path.split("?", 1)[0] != "/state":
                self.send_error(404)
                return
            run_id = self.headers.get("X-HookStorm-Run", "")
            with state_lock:
                state["verification_requests"].append(
                    {"run_id": run_id, "headers": dict(self.headers)}
                )
                processed = sum(1 for current_run, _ in state["effects"] if current_run == run_id)
            encoded = json.dumps({"processed_events": processed}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length)
            event_id = self.headers.get("X-Event-ID", "")
            run_id = self.headers.get("X-HookStorm-Run", "manual")
            delivery_key = (run_id, event_id)
            state["requests"].append(
                {"run_id": run_id, "event_id": event_id, "headers": dict(self.headers), "body": body}
            )
            state["attempts"][delivery_key] += 1
            if self.path == "/slow":
                with state_lock:
                    state["active"] += 1
                    state["max_active"] = max(state["max_active"], state["active"])
                time.sleep(0.08)
                with state_lock:
                    state["active"] -= 1
                status, payload = 200, {"ok": True}
            elif self.path == "/nested":
                status, payload = 200, {
                    "ok": True,
                    "metadata": {"delivery": {"accepted": True, "sequence": 7}},
                }
            elif self.path == "/flaky" and state["attempts"][delivery_key] == 1:
                status, payload = 503, {"retry": True}
            elif self.path.startswith("/signature/"):
                provider = self.path.rsplit("/", 1)[-1]
                secret = b"test-secret"
                valid = False
                if provider == "stripe":
                    parts = dict(
                        item.split("=", 1)
                        for item in self.headers.get("Stripe-Signature", "").split(",")
                        if "=" in item
                    )
                    try:
                        timestamp = int(parts.get("t", ""))
                    except ValueError:
                        timestamp = 0
                    if timestamp and abs(time.time() - timestamp) <= 300:
                        signed = str(timestamp).encode("ascii") + b"." + body
                        expected = hmac.new(secret, signed, hashlib.sha256).hexdigest()
                        valid = hmac.compare_digest(parts.get("v1", ""), expected)
                elif provider == "github":
                    expected = "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()
                    valid = hmac.compare_digest(
                        self.headers.get("X-Hub-Signature-256", ""), expected
                    )
                elif provider == "shopify":
                    expected = base64.b64encode(hmac.new(secret, body, hashlib.sha256).digest()).decode()
                    valid = hmac.compare_digest(
                        self.headers.get("X-Shopify-Hmac-Sha256", ""), expected
                    )
                elif provider == "standard":
                    standard_secret = b"hookstorm-test-standard-webhooks-secret"
                    message_id = self.headers.get("webhook-id", "")
                    timestamp_header = self.headers.get("webhook-timestamp", "")
                    try:
                        timestamp = int(timestamp_header)
                    except ValueError:
                        timestamp = 0
                    signed = f"{message_id}.{timestamp_header}.".encode() + body
                    expected = hmac.new(
                        standard_secret, signed, hashlib.sha256
                    ).digest()
                    supplied_signatures = self.headers.get("webhook-signature", "").split()
                    valid = (
                        message_id == event_id
                        and timestamp > 0
                        and abs(time.time() - timestamp) <= 300
                        and any(
                            version == "v1"
                            and hmac.compare_digest(
                                base64.b64decode(signature, validate=True), expected
                            )
                            for item in supplied_signatures
                            for version, separator, signature in [item.partition(",")]
                            if separator
                        )
                    )
                status = 200 if valid else 401
                if provider == "standard" and self.path == "/signature/standard-flaky" and state["attempts"][delivery_key] == 1 and valid:
                    status = 503
                payload = {"ok": valid, "provider": provider}
            elif self.path == "/signature":
                expected = "sha256=" + hmac.new(b"test-secret", body, hashlib.sha256).hexdigest()
                if self.headers.get("X-HookStorm-Signature") != expected:
                    status, payload = 401, {"ok": False}
                else:
                    status, payload = 200, {"ok": True}
            elif self.path == "/commit-then-drop":
                duplicate = delivery_key in state["effects"]
                state["seen"].add(delivery_key)
                state["effects"].add(delivery_key)
                if state["attempts"][delivery_key] == 1:
                    # Simulate a durable commit followed by a lost HTTP acknowledgement.
                    self.close_connection = True
                    try:
                        self.connection.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                    self.connection.close()
                    return
                status, payload = 200, {
                    "ok": True,
                    "duplicate": duplicate,
                    "effect_applied": not duplicate,
                }
            else:
                duplicate = delivery_key in state["seen"]
                state["seen"].add(delivery_key)
                state["effects"].add(delivery_key)
                status, payload = 200, {"ok": True, "duplicate": duplicate}

            encoded = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
