"""Deterministic request-body and HTTP/1.1 wire fault injection helpers."""

from __future__ import annotations

import contextvars
import gzip
import zlib
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import httpcore


WIRE_REQUEST_FAULTS = frozenset(
    {
        "chunked_transfer",
        "malformed_chunked",
        "partial_upload",
        "content_length_mismatch",
    }
)

_REQUEST_FAULT_CONTEXT: contextvars.ContextVar[tuple[str, int] | None] = (
    contextvars.ContextVar("hookstorm_request_fault", default=None)
)


def prepare_request_body(body: bytes, fault: str | None) -> tuple[bytes, dict[str, str]]:
    """Apply request payload/content-encoding variants before signature generation."""
    if fault == "malformed_json":
        return b"!" + body, {}
    if fault == "gzip_body":
        return gzip.compress(body, mtime=0), {"Content-Encoding": "gzip"}
    if fault == "deflate_body":
        return zlib.compress(body), {"Content-Encoding": "deflate"}
    if fault == "invalid_gzip":
        return b"\x1f\x8b\x08\x00hookstorm-invalid-gzip", {
            "Content-Encoding": "gzip"
        }
    if fault == "invalid_deflate":
        return b"\x78\x9chookstorm-invalid-deflate", {
            "Content-Encoding": "deflate"
        }
    if fault == "wrong_content_type":
        return body, {"Content-Type": "text/plain"}
    return body, {}


@contextmanager
def request_fault_context(fault: str, body_length: int) -> Iterator[None]:
    """Bind a wire-level fault to the connection opened by the current request task."""
    token = _REQUEST_FAULT_CONTEXT.set((fault, body_length))
    try:
        yield
    finally:
        _REQUEST_FAULT_CONTEXT.reset(token)


def _rewrite_headers(header_block: bytes, fault: str, body_length: int) -> bytes:
    lines = header_block.split(b"\r\n")
    request_line, raw_headers = lines[0], lines[1:]
    headers: list[bytes] = []
    replaced_length = False

    for line in raw_headers:
        if not line:
            continue
        name, separator, _value = line.partition(b":")
        if not separator:
            headers.append(line)
            continue
        lowered = name.strip().lower()
        if fault in {"chunked_transfer", "malformed_chunked"} and lowered in {
            b"content-length",
            b"transfer-encoding",
        }:
            continue
        if fault == "content_length_mismatch" and lowered == b"content-length":
            headers.append(f"Content-Length: {body_length + 1}".encode("ascii"))
            replaced_length = True
            continue
        if fault in {"partial_upload", "content_length_mismatch"} and lowered == b"connection":
            headers.append(b"Connection: close")
            continue
        headers.append(line)

    if fault in {"chunked_transfer", "malformed_chunked"}:
        headers.append(b"Transfer-Encoding: chunked")
    elif fault == "content_length_mismatch" and not replaced_length:
        headers.append(f"Content-Length: {body_length + 1}".encode("ascii"))

    if fault in {"partial_upload", "content_length_mismatch"}:
        headers.append(b"Connection: close")

    return b"\r\n".join([request_line, *headers, b"", b""])


class FaultInjectingNetworkStream:
    """Mutate HTTP/1.1 body framing after httpcore has serialized the request."""

    def __init__(self, stream: Any, fault: str, body_length: int) -> None:
        self._stream = stream
        self._fault = fault
        self._body_length = body_length
        self._headers_sent = False
        self._body_bytes_sent = 0
        self._closed = False
        self._pending_headers = bytearray()

    async def write(self, buffer: bytes, timeout: float | None = None) -> None:
        if self._closed:
            raise httpcore.WriteError("request fault closed the network stream")
        if not buffer:
            return

        if not self._headers_sent:
            self._pending_headers.extend(buffer)
            marker = b"\r\n\r\n"
            header_bytes = bytes(self._pending_headers)
            split_at = header_bytes.find(marker)
            if split_at < 0:
                return
            header_block = header_bytes[:split_at]
            remainder = header_bytes[split_at + len(marker):]
            rewritten = _rewrite_headers(
                header_block,
                self._fault,
                self._body_length,
            )
            await self._stream.write(rewritten, timeout=timeout)
            self._headers_sent = True
            self._pending_headers.clear()
            if remainder:
                await self._write_body(remainder, timeout)
            return

        await self._write_body(buffer, timeout)

    async def _write_body(self, buffer: bytes, timeout: float | None) -> None:
        if self._fault == "chunked_transfer":
            self._body_bytes_sent += len(buffer)
            chunk = f"{len(buffer):X}\r\n".encode("ascii") + buffer + b"\r\n"
            if self._body_bytes_sent >= self._body_length:
                chunk += b"0\r\n\r\n"
            await self._stream.write(chunk, timeout=timeout)
            return

        if self._fault == "malformed_chunked":
            if self._body_bytes_sent == 0:
                self._body_bytes_sent += len(buffer)
                await self._stream.write(
                    b"Z\r\n" + buffer + b"\r\n0\r\n\r\n",
                    timeout=timeout,
                )
            return

        if self._fault == "partial_upload":
            partial_length = max(0, self._body_length // 2)
            remaining = max(0, partial_length - self._body_bytes_sent)
            if remaining:
                await self._stream.write(buffer[:remaining], timeout=timeout)
                self._body_bytes_sent += min(len(buffer), remaining)
            await self._close_and_fail("injected partial request upload")
            return

        if self._fault == "content_length_mismatch":
            await self._stream.write(buffer, timeout=timeout)
            self._body_bytes_sent += len(buffer)
            if self._body_bytes_sent >= self._body_length:
                await self._close_and_fail(
                    "injected Content-Length larger than the transmitted request body"
                )
            return

        await self._stream.write(buffer, timeout=timeout)

    async def _close_and_fail(self, message: str) -> None:
        self._closed = True
        await self._stream.aclose()
        raise httpcore.WriteError(message)

    async def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        return await self._stream.read(max_bytes, timeout=timeout)

    async def start_tls(
        self,
        ssl_context: Any,
        server_hostname: str | None = None,
        timeout: float | None = None,
    ) -> FaultInjectingNetworkStream:
        self._stream = await self._stream.start_tls(
            ssl_context,
            server_hostname=server_hostname,
            timeout=timeout,
        )
        return self

    def get_extra_info(self, info: str) -> Any:
        return self._stream.get_extra_info(info)

    async def aclose(self) -> None:
        if not self._closed:
            self._closed = True
            await self._stream.aclose()


def wrap_network_stream(stream: Any) -> Any:
    """Wrap a newly opened stream when the current task is injecting a wire fault."""
    context = _REQUEST_FAULT_CONTEXT.get()
    if context is None:
        return stream
    fault, body_length = context
    return FaultInjectingNetworkStream(stream, fault, body_length)
