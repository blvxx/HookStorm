from __future__ import annotations

import json
import threading
from collections import defaultdict
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest


@pytest.fixture
def receiver() -> Iterator[tuple[str, dict]]:
    state = {"seen": set(), "attempts": defaultdict(int), "requests": []}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: object) -> None:
            return

        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length)
            event_id = self.headers.get("X-Event-ID", "")
            state["requests"].append({"event_id": event_id, "headers": dict(self.headers), "body": body})
            state["attempts"][event_id] += 1
            if self.path == "/flaky" and state["attempts"][event_id] == 1:
                status, payload = 503, {"retry": True}
            elif self.path == "/signature":
                import hashlib
                import hmac

                expected = "sha256=" + hmac.new(b"test-secret", body, hashlib.sha256).hexdigest()
                if self.headers.get("X-HookStorm-Signature") != expected:
                    status, payload = 401, {"ok": False}
                else:
                    status, payload = 200, {"ok": True}
            else:
                duplicate = event_id in state["seen"]
                state["seen"].add(event_id)
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
