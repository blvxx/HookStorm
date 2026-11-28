"""One-command local demonstration of HookStorm's retry handling."""

from __future__ import annotations

import asyncio
import json
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from hookstorm.config import load_scenario
from hookstorm.report import write_report
from hookstorm.runner import run_scenario


class _DemoHandler(BaseHTTPRequestHandler):
    """Tiny receiver that returns one retryable error per delivery."""

    server_version = "HookStormDemo/0.1"

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def _reply(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
        if self.path != "/webhook":
            self._reply(404, {"accepted": False, "error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length < 0 or length > 1024 * 1024:
                self._reply(413, {"accepted": False, "error": "invalid request size"})
                return
            json.loads(self.rfile.read(length))
            attempt = int(self.headers.get("X-HookStorm-Attempt", "1"))
        except (ValueError, json.JSONDecodeError):
            self._reply(400, {"accepted": False, "error": "invalid request"})
            return
        if attempt == 1:
            self._reply(503, {"accepted": False, "retry": True})
            return
        self._reply(200, {"accepted": True})


def run_demo(
    *,
    output: str | Path | None = None,
    report_format: str = "html",
) -> int:
    """Start a loopback-only receiver, run a retry + duplicate example, and write a report."""
    if report_format not in {"html", "json", "junit"}:
        raise ValueError("demo report format must be html, json, or junit")
    suffix = {"html": ".html", "json": ".json", "junit": ".xml"}[report_format]
    report_path = Path(output or f"hookstorm-demo-report{suffix}")
    scenario_text = """version: 1
name: HookStorm local demo
description: Demonstrates automatic retries and duplicate deliveries against a temporary local receiver.
retry:
  max_attempts: 2
  backoff_ms: 50
  retry_on: [503]
delivery:
  duplicate_count: 1
events:
  - id: order.created
    event_id: evt_hookstorm_demo_order_001
    payload:
      order_id: demo-order-001
      amount: 1250
      currency: USD
    expect_status: [200]
    expect_json:
      accepted: true
"""
    with tempfile.TemporaryDirectory(prefix="hookstorm-demo-") as directory:
        scenario_path = Path(directory) / "demo.yaml"
        scenario_path.write_text(scenario_text, encoding="utf-8")
        scenario = load_scenario(scenario_path)

        server = ThreadingHTTPServer(("127.0.0.1", 0), _DemoHandler)
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, name="hookstorm-demo", daemon=True)
        thread.start()
        try:
            target = f"http://127.0.0.1:{server.server_port}/webhook"
            result = asyncio.run(run_scenario(scenario, target))
            write_report(result, report_path, report_format)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    summary = result["summary"]
    state = "PASS" if summary["success"] else "FAIL"
    print(f"HookStorm demo · {state}")
    print(
        f"Deliveries: {summary['passed']}/{summary['total_deliveries']} passed · "
        f"HTTP attempts: {summary['attempts']} · Duration: {result['duration_ms']:.0f} ms"
    )
    print("This demo ran against a temporary 127.0.0.1 receiver; no external endpoint was contacted.")
    print(f"Report: {report_path.resolve()}")
    return 0 if summary["success"] else 1
