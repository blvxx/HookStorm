from __future__ import annotations

import asyncio
import gzip
import json
import threading
import zlib
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from hookstorm.config import ScenarioError, validate_scenario
from hookstorm.runner import run_scenario


@pytest.fixture
def request_receiver() -> Iterator[tuple[str, dict]]:
    state: dict = {
        "requests": [],
        "completed": threading.Event(),
        "invalid": [],
    }
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: object) -> None:
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

        def _read_body(self) -> tuple[bytes, str | None]:
            transfer_encoding = self.headers.get("Transfer-Encoding", "").strip().lower()
            length_header = self.headers.get("Content-Length")
            if transfer_encoding:
                if transfer_encoding != "chunked" or length_header is not None:
                    return b"", "conflicting request framing"
                chunks = bytearray()
                while True:
                    line = self.rfile.readline(8192)
                    if not line.endswith(b"\r\n"):
                        return bytes(chunks), "truncated chunk-size line"
                    try:
                        size = int(line[:-2].split(b";", 1)[0].strip(), 16)
                    except ValueError:
                        return bytes(chunks), "invalid chunk-size token"
                    if size == 0:
                        while True:
                            trailer = self.rfile.readline(8192)
                            if trailer == b"\r\n":
                                return bytes(chunks), None
                            if not trailer or not trailer.endswith(b"\r\n"):
                                return bytes(chunks), "truncated chunk trailer"
                    chunk = self.rfile.read(size)
                    if len(chunk) != size:
                        return bytes(chunks + chunk), "incomplete chunk data"
                    if self.rfile.read(2) != b"\r\n":
                        return bytes(chunks + chunk), "invalid chunk terminator"
                    chunks.extend(chunk)
                return bytes(chunks), None

            try:
                length = int(length_header or "0")
            except ValueError:
                return b"", "invalid Content-Length"
            raw = self.rfile.read(length)
            if len(raw) != length:
                return raw, "request body ended before Content-Length bytes arrived"
            return raw, None

        def do_POST(self) -> None:  # noqa: N802
            raw, framing_error = self._read_body()
            record = {
                "headers": dict(self.headers),
                "raw_body": raw,
                "payload": None,
                "error": framing_error,
            }
            status = 200
            error = framing_error
            payload = None

            if error is None:
                content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
                if content_type != "application/json":
                    status, error = 415, "Content-Type must be application/json"
                else:
                    encoding = self.headers.get("Content-Encoding", "identity").strip().lower()
                    try:
                        if encoding in {"", "identity"}:
                            decoded = raw
                        elif encoding == "gzip":
                            decoded = gzip.decompress(raw)
                        elif encoding == "deflate":
                            decoded = zlib.decompress(raw)
                        else:
                            status, error = 415, f"unsupported Content-Encoding: {encoding}"
                            decoded = b""
                    except (OSError, EOFError, zlib.error):
                        status, error, decoded = 400, "invalid compressed body", b""
                    if error is None:
                        try:
                            payload = json.loads(decoded)
                        except (UnicodeDecodeError, json.JSONDecodeError):
                            status, error = 400, "malformed JSON body"

            record["payload"] = payload
            record["error"] = error
            with lock:
                state["requests"].append(record)
                if error is not None:
                    state["invalid"].append(record)

            if error is not None:
                self._reply(status, {"ok": False, "error": error})
            else:
                self._reply(200, {"ok": True, "payload": payload})
            state["completed"].set()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _scenario(fault_field: str | None = None, *, attempts: int = 1) -> dict:
    delivery = {"fault_injection": {fault_field: [1]}} if fault_field else {}
    return validate_scenario(
        {
            "version": 1,
            "name": "request fault test",
            "retry": {
                "max_attempts": attempts,
                "backoff_ms": 1,
                "retry_on": [400, 415, 500, 502, 503, 504],
            },
            "events": [
                {
                    "id": "request.test",
                    "event_id": "evt_request_fault",
                    "payload": {"kind": "request-fault", "value": 7},
                    "delivery": delivery,
                }
            ],
        }
    )


@pytest.mark.parametrize(
    ("field", "fault", "status"),
    [
        ("malformed_json_attempts", "malformed_json", 400),
        ("wrong_content_type_attempts", "wrong_content_type", 415),
        ("invalid_gzip_attempts", "invalid_gzip", 400),
        ("invalid_deflate_attempts", "invalid_deflate", 400),
        ("malformed_chunked_attempts", "malformed_chunked", 400),
    ],
)
def test_invalid_request_variants_are_rejected_then_retry_cleanly(
    request_receiver,
    field: str,
    fault: str,
    status: int,
) -> None:
    target, state = request_receiver
    result = asyncio.run(
        run_scenario(_scenario(field, attempts=2), target)
    )

    attempts = result["deliveries"][0]["attempts"]
    assert result["summary"]["success"] is True
    assert [item["status_code"] for item in attempts] == [status, 200]
    assert attempts[0]["injected_fault"] == fault
    assert attempts[0]["passed"] is False
    assert attempts[1]["passed"] is True
    assert state["completed"].wait(1)
    assert len(state["requests"]) == 2
    assert state["requests"][1]["payload"] == {
        "kind": "request-fault",
        "value": 7,
    }


@pytest.mark.parametrize(
    ("field", "fault", "encoding"),
    [
        ("gzip_body_attempts", "gzip_body", "gzip"),
        ("deflate_body_attempts", "deflate_body", "deflate"),
        ("chunked_transfer_attempts", "chunked_transfer", None),
    ],
)
def test_valid_compressed_and_chunked_request_variants_are_decoded(
    request_receiver,
    field: str,
    fault: str,
    encoding: str | None,
) -> None:
    target, state = request_receiver
    result = __import__("asyncio").run(
        run_scenario(_scenario(field), target)
    )

    attempt = result["deliveries"][0]["attempts"][0]
    assert result["summary"]["success"] is True
    assert attempt["status_code"] == 200
    assert attempt["injected_fault"] == fault
    assert state["completed"].wait(1)
    record = state["requests"][0]
    assert record["payload"] == {"kind": "request-fault", "value": 7}
    if encoding is not None:
        assert record["headers"].get("Content-Encoding") == encoding
    else:
        assert record["headers"].get("Transfer-Encoding", "").lower() == "chunked"
        assert "Content-Length" not in record["headers"]


@pytest.mark.parametrize(
    ("field", "fault"),
    [
        ("partial_upload_attempts", "partial_upload"),
        ("content_length_mismatch_attempts", "content_length_mismatch"),
    ],
)
def test_truncated_and_mismatched_length_uploads_fail_safely_then_retry(
    request_receiver,
    field: str,
    fault: str,
) -> None:
    target, state = request_receiver
    result = __import__("asyncio").run(
        run_scenario(_scenario(field, attempts=2), target)
    )

    attempts = result["deliveries"][0]["attempts"]
    assert result["summary"]["success"] is True
    assert attempts[0]["injected_fault"] == fault
    assert attempts[0]["status_code"] is None
    assert "injected" in attempts[0]["error"].lower()
    assert attempts[1]["status_code"] == 200
    assert attempts[1]["passed"] is True
    assert state["completed"].wait(1)
    assert len(state["requests"]) == 2
    assert state["requests"][0]["error"] == (
        "request body ended before Content-Length bytes arrived"
    )
    assert state["requests"][1]["payload"] == {
        "kind": "request-fault",
        "value": 7,
    }


def test_request_faults_cannot_be_combined_on_the_same_attempt() -> None:
    with pytest.raises(ScenarioError, match="cannot inject more than one fault"):
        validate_scenario(
            {
                "version": 1,
                "name": "conflicting request faults",
                "events": [
                    {
                        "id": "request.test",
                        "event_id": "evt_conflict",
                        "payload": {},
                        "delivery": {
                            "fault_injection": {
                                "malformed_json_attempts": [1],
                                "partial_upload_attempts": [1],
                            }
                        },
                    }
                ],
            }
        )
