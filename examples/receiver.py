"""A tiny local webhook receiver for HookStorm examples (stdlib only)."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import socket
import sqlite3
import threading
import time
from collections import defaultdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

SEEN: set[tuple[str, str]] = set()
ATTEMPTS: defaultdict[tuple[str, str], int] = defaultdict(int)
SECRET = os.getenv("HOOKSTORM_SECRET", "")
# Public demo-only key. Never reuse this sample key in a real integration.
STANDARD_WEBHOOKS_DEMO_KEY = b"hookstorm-standard-webhooks-demo-key-32"
STANDARD_WEBHOOKS_DEMO_SECRET = (
    "whsec_" + base64.b64encode(STANDARD_WEBHOOKS_DEMO_KEY).decode("ascii")
)
BUSINESS_DB = sqlite3.connect(":memory:", check_same_thread=False)
BUSINESS_LOCK = threading.Lock()
with BUSINESS_LOCK:
    BUSINESS_DB.execute(
        """
        CREATE TABLE processed_events (
            run_id TEXT NOT NULL,
            event_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            PRIMARY KEY (run_id, event_id)
        )
        """
    )
    BUSINESS_DB.commit()


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

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        if urlsplit(self.path).path != "/__hookstorm/state":
            self._reply(404, {"ok": False, "error": "not found"})
            return
        run_id = self.headers.get("X-HookStorm-Run", "")
        with BUSINESS_LOCK:
            row = BUSINESS_DB.execute(
                "SELECT COUNT(*) FROM processed_events WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        self._reply(200, {"ok": True, "processed_events": int(row[0])})

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length)
        event_id = self.headers.get("X-Event-ID", "missing-id")
        run_id = self.headers.get("X-HookStorm-Run", "manual")
        delivery_key = (run_id, event_id)

        if self.path.startswith("/webhook/slow"):
            time.sleep(1.0)

        if (
            (SECRET or self.path.startswith("/webhook/signature/standard"))
            and self.path.startswith("/webhook/signature")
        ):
            if self.path.startswith("/webhook/signature/standard"):
                message_id = self.headers.get("webhook-id", "")
                timestamp_header = self.headers.get("webhook-timestamp", "")
                try:
                    timestamp = int(timestamp_header)
                except ValueError:
                    timestamp = 0
                signed = f"{message_id}.{timestamp_header}.".encode("utf-8") + raw
                expected = hmac.new(
                    STANDARD_WEBHOOKS_DEMO_KEY, signed, hashlib.sha256
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
            elif self.path.startswith("/webhook/signature/stripe"):
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
                error_payload = {"ok": False, "error": "invalid signature"}
                if self.path.startswith("/webhook/signature/standard"):
                    error_payload["provider"] = "standard"
                self._reply(401, error_payload)
                return

        ATTEMPTS[delivery_key] += 1
        if self.path.startswith("/webhook/flaky") and ATTEMPTS[delivery_key] == 1:
            self._reply(503, {"ok": False, "retry": True})
            return

        duplicate = delivery_key in SEEN
        SEEN.add(delivery_key)
        with BUSINESS_LOCK:
            cursor = BUSINESS_DB.execute(
                """
                INSERT OR IGNORE INTO processed_events (run_id, event_id, event_type)
                VALUES (?, ?, ?)
                """,
                (run_id, event_id, self.headers.get("X-Event-Type", "unknown")),
            )
            BUSINESS_DB.commit()

        if (
            self.path.startswith("/webhook/commit-then-drop")
            and ATTEMPTS[delivery_key] == 1
        ):
            # The business write is durable, but the acknowledgement never reaches the sender.
            print(
                f"[receiver] committed {event_id} for run {run_id}, then dropped the response"
            )
            self.close_connection = True
            try:
                self.connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self.connection.close()
            return

        success_payload = {
            "ok": True,
            "event_id": event_id,
            "duplicate": duplicate,
            "effect_applied": cursor.rowcount == 1,
        }
        if self.path.startswith("/webhook/signature/standard"):
            success_payload["provider"] = "standard"
        self._reply(200, success_payload)


def main() -> None:
    address = ("127.0.0.1", 8000)
    print(f"HookStorm demo receiver listening on http://{address[0]}:{address[1]}")
    print(
        "Endpoints: /webhook, /webhook/flaky, /webhook/slow, "
        "/webhook/commit-then-drop, /webhook/signature"
    )
    print(f"Standard Webhooks demo secret: {STANDARD_WEBHOOKS_DEMO_SECRET}")
    print("State probe: GET /__hookstorm/state (scoped by X-HookStorm-Run)")
    print("Provider signatures: /webhook/signature/stripe, /github, /shopify, /standard")
    print("Standard Webhooks validation enabled with the public demo-only key")
    if SECRET:
        print("HMAC validation enabled for generic, Stripe, GitHub, and Shopify endpoints")
    with ThreadingHTTPServer(address, Handler) as server:
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("\nStopping receiver...")


if __name__ == "__main__":
    main()
