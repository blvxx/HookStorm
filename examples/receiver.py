"""A tiny local webhook receiver for HookStorm examples (stdlib only)."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from collections import defaultdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SEEN: set[str] = set()
ATTEMPTS: defaultdict[str, int] = defaultdict(int)
SECRET = os.getenv("HOOKSTORM_SECRET", "")


class Handler(BaseHTTPRequestHandler):
    server_version = "HookStormDemo/0.1"

    def log_message(self, fmt: str, *args: object) -> None:
        print("[receiver] " + fmt % args)

    def _reply(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length)
        event_id = self.headers.get("X-Event-ID", "missing-id")

        if self.path.startswith("/webhook/slow"):
            time.sleep(1.0)

        if SECRET and self.path.startswith("/webhook/signature"):
            supplied = self.headers.get("X-HookStorm-Signature", "")
            expected = "sha256=" + hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest()
            if not hmac.compare_digest(supplied, expected):
                self._reply(401, {"ok": False, "error": "invalid signature"})
                return

        ATTEMPTS[event_id] += 1
        if self.path.startswith("/webhook/flaky") and ATTEMPTS[event_id] == 1:
            self._reply(503, {"ok": False, "retry": True})
            return

        duplicate = event_id in SEEN
        SEEN.add(event_id)
        self._reply(200, {"ok": True, "event_id": event_id, "duplicate": duplicate})


def main() -> None:
    address = ("127.0.0.1", 8000)
    print(f"HookStorm demo receiver listening on http://{address[0]}:{address[1]}")
    print("Endpoints: /webhook, /webhook/flaky, /webhook/slow, /webhook/signature")
    if SECRET:
        print("HMAC validation enabled for /webhook/signature")
    with ThreadingHTTPServer(address, Handler) as server:
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("\nStopping receiver...")


if __name__ == "__main__":
    main()
