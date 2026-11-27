# Request and transport fault injection

HookStorm can now exercise request variants and malformed HTTP/1.1 traffic on a per-attempt basis. These are explicit scenario options, not ambient network chaos.

## Quick start

Run the request-level fault matrix against the local demo receiver:

~~~bash
python examples/receiver.py
~~~

In another terminal:

~~~bash
hookstorm validate examples/request-level-faults.yaml
hookstorm run examples/request-level-faults.yaml \
  --target http://127.0.0.1:8000/webhook
~~~

The sample includes retries so a deliberately rejected or truncated first attempt is followed by a clean delivery. It is suitable for a local receiver or an isolated staging endpoint.

## Supported per-attempt request variants

Add one or more attempt lists under an event's `delivery.fault_injection`:

| Field | Wire behavior | Typical receiver response |
|---|---|---|
| `malformed_json_attempts` | Prefixes the JSON body with an invalid token | 400 |
| `wrong_content_type_attempts` | Sends the JSON bytes as `text/plain` | 415 |
| `gzip_body_attempts` | Compresses JSON with gzip and adds `Content-Encoding: gzip` | 2xx when supported |
| `deflate_body_attempts` | Compresses JSON with zlib/deflate and adds `Content-Encoding: deflate` | 2xx when supported |
| `invalid_gzip_attempts` | Labels an intentionally invalid body as gzip | 400 |
| `invalid_deflate_attempts` | Labels an intentionally invalid body as deflate | 400 |
| `chunked_transfer_attempts` | Reframes the request body as a valid HTTP/1.1 chunked message | 2xx when supported |
| `malformed_chunked_attempts` | Sends an invalid chunk-size token | 400 |
| `partial_upload_attempts` | Sends only the first half of the request body, then closes the connection | Client write error / incomplete request |
| `content_length_mismatch_attempts` | Declares one more byte than is sent, then closes the connection | Client write error / incomplete request |

Existing `connect_error_attempts`, `response_loss_attempts`, and `response_body_error_attempts` remain supported.

All attempt numbers are integers from 1 to 10. A given attempt can have only one injected fault; config validation rejects overlapping plans before network traffic is sent. The attempt number restarts for each duplicate delivery.

## Request signing and retries

Body transformations are applied before the request signature is generated. For gzip and deflate, HookStorm signs the compressed bytes actually sent, matching the raw-body signature model. A malformed or truncated body cannot be fully verified by the receiver, as intended.

A request fault is applied only to the specified attempt. To test recovery, set `retry.max_attempts` above that attempt and include any expected HTTP rejection code in `retry.retry_on` (for example, 400 or 415). Client-side write failures are retryable using the normal retry loop.

## Safety and implementation boundaries

- The wire-level framing faults target HTTP/1.1. HTTP/2 frame corruption is not implemented.
- Chunked transfer, partial upload, and Content-Length mismatch use a dedicated no-keepalive transport so each injected fault is isolated to a fresh connection.
- DNS pinning remains active; HTTPS uses the validated IP while keeping the original hostname for SNI and certificate validation.
- Body and header mutations happen only for the attempt selected by the scenario. Ordinary requests retain the existing HTTPX transport path.
- Partial uploads and length mismatches deliberately close a socket while the receiver may still be reading. Use loopback or an isolated system that you own and can safely reset.
- These faults do not emulate arbitrary packet loss, TCP retransmission behavior, slow streaming bodies, secret rotation during a run, malformed HTTP/2, or kernel/network-device failures.

Reports identify the injected fault on the attempt. Payloads, request headers, response bodies, and secrets remain excluded from reports and history.
