# FastAPI and Flask integration

HookStorm provides optional test-receiver adapters for application repositories. They let an application expose a small JSON endpoint during local development or isolated integration tests, then inspect what HookStorm delivered through an in-memory recorder.

These adapters are test utilities. They do not replace your application's real webhook handler, verify provider signatures, persist events, or provide a production queue.

## Install only the framework you use

~~~bash
python -m pip install "hookstorm[fastapi]"
# or
python -m pip install "hookstorm[flask]"
~~~

The core `hookstorm` package does not install FastAPI or Flask. Importing `hookstorm.integrations` also does not import either framework.

## FastAPI

Create a router and include it in a test application:

~~~python
from fastapi import FastAPI
from hookstorm.integrations.fastapi import create_router

hookstorm_endpoint = create_router()
app = FastAPI()
app.include_router(hookstorm_endpoint.router)
~~~

Start the app with your normal ASGI server, for example Uvicorn, and point HookStorm at the route:

~~~bash
hookstorm run examples/payment-retry.yaml \
  --target http://127.0.0.1:8000/__hookstorm/webhook
~~~

Inspect the requests in a test:

~~~python
def test_retry_is_recorded():
    records = hookstorm_endpoint.recorder.events

    assert len(records) == 2
    assert records[0].event_id == "evt_example"
    assert records[1].duplicate is True
    assert records[0].payload["type"] == "invoice.paid"
~~~

The factory returns a `FastAPIWebhookEndpoint` with two attributes: `router` and `recorder`. You can supply your own `WebhookRecorder` or configure `max_body_bytes`, `max_total_body_bytes`, `max_events`, and `include_sensitive_headers`.

## Flask

Register the endpoint on an existing application:

~~~python
from flask import Flask
from hookstorm.integrations.flask import register_endpoint

app = Flask(__name__)
hookstorm_recorder = register_endpoint(app)

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8000)
~~~

Then use the same HookStorm target:

~~~bash
hookstorm run examples/payment-retry.yaml \
  --target http://127.0.0.1:8000/__hookstorm/webhook
~~~

The function returns the `WebhookRecorder`. Use `hookstorm_recorder.events`, `snapshot()`, and `clear()` in your tests.

## Shared recorder behavior

Both adapters use the same framework-independent `WebhookRecorder`.

| Behavior | Contract |
|---|---|
| Route | POST `/__hookstorm/webhook` by default |
| Accepted body | UTF-8 JSON and `application/*+json` |
| Encodings | identity, gzip, and deflate |
| Default size limit | 1 MiB for both wire and decoded body |
| Default event capacity | 1,000 in-memory records; HTTP 429 when full |\n| Default aggregate body budget | 8 MiB total across wire and decoded body bytes; HTTP 429 when exhausted |\n| Header budget | 64 headers / 16 KiB; HTTP 431 when exceeded |
| Duplicate detection | `X-HookStorm-Run` plus event ID when supplied |
| Default sensitive-header handling | Authorization, API keys, tokens, cookies, credentials, and signature-like header names are redacted |
| Storage | In-memory only; cleared when the process exits |
| Invalid JSON / duplicate JSON keys | HTTP 400 |
| Unsupported content type / encoding | HTTP 415 |
| Body too large | HTTP 413 |
| Recorder capacity reached | HTTP 429; call `clear()` between test runs |

Both adapters enforce the wire-body limit while reading the incoming request: FastAPI consumes the ASGI request stream incrementally, and Flask reads no more than `max_body_bytes + 1` bytes. An oversized request is rejected before the full body is accumulated or retained by the recorder. The recorder applies the same cap again and separately limits the decoded gzip/deflate body. It also enforces an aggregate 8 MiB budget across both wire and decoded request bodies, in addition to the event-count cap, so a long-lived test process cannot retain an unbounded number of maximum-size requests. Header count and total header bytes are capped before a record is retained.

The recorder preserves the original request bytes in `record.raw_body`, including compressed bytes, while `record.payload` contains decoded JSON. Sensitive headers are redacted in `record.headers` unless you explicitly set `include_sensitive_headers=True`.

A record includes `event_id`, `run_id`, `duplicate`, `payload`, `raw_body`, `headers`, and `received_at`. The aggregate `snapshot()` exposes counters and event IDs without payloads or headers.

## Safety and limitations

- Use these endpoints only in local development or an isolated test application. Do not register them in a production app.
- Bind local development servers to `127.0.0.1`; do not expose test routes publicly.
- Use synthetic payloads. The recorder keeps request bodies in process memory so tests can inspect them.
- Sensitive headers are redacted by default using both known names and secret-like name fragments; custom headers can still carry credentials under names HookStorm cannot recognize. Payloads may also contain personal or confidential data.
- The helper does not verify HMAC signatures. Signature verification remains the responsibility of the application handler under test.
- The recorder is not durable, distributed, or designed for concurrent production traffic.
- Your test suite remains responsible for starting and stopping the app server and any databases, queues, or external dependencies.

See the [pytest integration guide](pytest-integration.md) for running HookStorm scenarios from application tests.
