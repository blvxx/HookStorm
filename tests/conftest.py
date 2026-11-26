from __future__ import annotations

import base64
import hashlib
import hmac
import json
import threading
import time
from collections import defaultdict
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest


@pytest.fixture
def receiver() -> Iterator[tuple[str, dict]]:
    state = {"seen": set(), "attempts": defaultdict(int), "requests": [], "active": 0, "max_active": 0}
    state_lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: object) -> None:
            return

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
                status = 200 if valid else 401
                payload = {"ok": valid, "provider": provider}
            elif self.path == "/signature":
                expected = "sha256=" + hmac.new(b"test-secret", body, hashlib.sha256).hexdigest()
                if self.headers.get("X-HookStorm-Signature") != expected:
                    status, payload = 401, {"ok": False}
                else:
                    status, payload = 200, {"ok": True}
            else:
                duplicate = delivery_key in state["seen"]
                state["seen"].add(delivery_key)
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
