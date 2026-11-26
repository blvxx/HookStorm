"""A tiny local webhook receiver for HookStorm examples (stdlib only)."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from collections import defaultdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SEEN: set[tuple[str, str]] = set()
ATTEMPTS: defaultdict[tuple[str, str], int] = defaultdict(int)
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
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            # Expected when HookStorm intentionally times out a slow delivery.
            pass

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length)
        event_id = self.headers.get("X-Event-ID", "missing-id")
        run_id = self.headers.get("X-HookStorm-Run", "manual")
        delivery_key = (run_id, event_id)

        if self.path.startswith("/webhook/slow"):
            time.sleep(1.0)

        if SECRET and self.path.startswith("/webhook/signature"):
            if self.path.startswith("/webhook/signature/stripe"):
                supplied = self.headers.get("Stripe-Signature", "")
                parts: dict[str, list[str]] = defaultdict(list)
                for item in supplied.split(","):
                    key, separator, value = item.partition("=")
                    if separator:
                        parts[key.strip()].append(value.strip())
                try:
                    timestamp = int(parts.get("t", [""])[0])
                except ValueError:
                    timestamp = 0
                signed = str(timestamp).encode("ascii") + b"." + raw
                expected = hmac.new(SECRET.encode(), signed, hashlib.sha256).hexdigest()
                valid = (
                    timestamp > 0
                    and abs(time.time() - timestamp) <= 300
                    and any(hmac.compare_digest(value, expected) for value in parts.get("v1", []))
                )
            elif self.path.startswith("/webhook/signature/github"):
                supplied = self.headers.get("X-Hub-Signature-256", "")
                expected = "sha256=" + hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest()
                valid = hmac.compare_digest(supplied, expected)
            elif self.path.startswith("/webhook/signature/shopify"):
                supplied = self.headers.get("X-Shopify-Hmac-Sha256", "")
                expected = base64.b64encode(
                    hmac.new(SECRET.encode(), raw, hashlib.sha256).digest()
                ).decode("ascii")
                valid = hmac.compare_digest(supplied, expected)
            else:
                supplied = self.headers.get("X-HookStorm-Signature", "")
                expected = "sha256=" + hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest()
                valid = hmac.compare_digest(supplied, expected)
            if not valid:
                self._reply(401, {"ok": False, "error": "invalid signature"})
                return

        ATTEMPTS[delivery_key] += 1
        if self.path.startswith("/webhook/flaky") and ATTEMPTS[delivery_key] == 1:
            self._reply(503, {"ok": False, "retry": True})
            return

        duplicate = delivery_key in SEEN
        SEEN.add(delivery_key)
        self._reply(200, {"ok": True, "event_id": event_id, "duplicate": duplicate})


def main() -> None:
    address = ("127.0.0.1", 8000)
    print(f"HookStorm demo receiver listening on http://{address[0]}:{address[1]}")
    print("Endpoints: /webhook, /webhook/flaky, /webhook/slow, /webhook/signature")
    print("Provider signatures: /webhook/signature/stripe, /github, /shopify")
    if SECRET:
        print("HMAC validation enabled for generic, Stripe, GitHub, and Shopify endpoints")
    with ThreadingHTTPServer(address, Handler) as server:
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("\nStopping receiver...")


if __name__ == "__main__":
    main()
