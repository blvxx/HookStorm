# HookStorm

**Break your webhook delivery flow on purpose — before production does.**

A local-first chaos testing CLI for retries, duplicate events, out-of-order delivery, timeouts, and HMAC signature failures.

[![CI](https://github.com/blvxx/HookStorm/actions/workflows/ci.yml/badge.svg)](https://github.com/blvxx/HookStorm/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.11%2B-3776AB)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

## Why HookStorm?

Webhook integrations fail in ways that are easy to miss in happy-path tests:

- A provider retries after a 500, but your handler processes the same payment twice.
- Events arrive out of order and overwrite newer state with stale data.
- A signature is missing, invalid, or calculated from the wrong request bytes.
- A slow handler exceeds the sender's timeout, even though it eventually finishes.
- A receiver returns 429 or 503, and retry behavior is not what you expected.

HookStorm sends controlled webhook traffic to **your own endpoint** and checks responses against expectations. It is a test harness, not a hosted webhook inbox or production delivery service.

## Capabilities

| Failure mode | Scenario setting |
|---|---|
| Retry after transient HTTP errors | retry.max_attempts, retry.retry_on, retry.backoff_ms |
| Duplicate event delivery | delivery.duplicate_count |
| Out-of-order delivery | delivery.order: reverse |
| Concurrent event delivery | delivery.concurrency: 1–50 (default: 1) |
| Slow receiver / timeout | delivery.timeout_ms |
| HMAC signature | delivery.signature: valid / invalid / missing |
| Expected HTTP status | expect_status |
| Nested JSON response assertions | expect_json, expect_duplicate_json |

The same event_id is preserved across retries and duplicate deliveries, allowing an idempotency implementation to be checked rather than merely sending unrelated repeated requests. Set `delivery.concurrency` from 1 to 50 to run independent events concurrently; retries and duplicate deliveries for each event remain ordered, and reports retain scenario order.

## Quick start

Requires Python 3.11 or newer.

~~~bash
git clone https://github.com/blvxx/HookStorm.git
cd HookStorm
python -m venv .venv
source .venv/bin/activate
python -m pip install -e .
~~~

**Windows PowerShell:**

~~~powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e .
~~~

If PowerShell blocks activation, allow it for the current terminal only:

~~~powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\.venv\Scripts\Activate.ps1
~~~

Start the included demo receiver in a second terminal:

~~~bash
python examples/receiver.py
~~~

Run a retry scenario against its flaky endpoint:

~~~bash
hookstorm run examples/payment-retry.yaml --target http://127.0.0.1:8000/webhook/flaky
~~~

HookStorm writes hookstorm-report.html by default. Open it in a browser to inspect delivery outcomes and attempt counts.

Validate a scenario without sending requests:

~~~bash
hookstorm validate examples/duplicate-delivery.yaml
~~~

## Try the scenarios

~~~bash
hookstorm run examples/duplicate-delivery.yaml --target http://127.0.0.1:8000/webhook
hookstorm run examples/out-of-order.yaml --target http://127.0.0.1:8000/webhook
hookstorm run examples/timeout.yaml --target http://127.0.0.1:8000/webhook/slow
~~~

The timeout example is intentionally expected to fail: it proves that HookStorm detects a receiver that takes longer than the configured timeout.

For HMAC examples, set the same development secret in both terminals. The demo receiver checks signatures on /webhook/signature when HOOKSTORM_SECRET is set.

~~~bash
# Linux / macOS
export HOOKSTORM_SECRET='local-demo-secret'
# Windows PowerShell
$env:HOOKSTORM_SECRET = 'local-demo-secret'
hookstorm run examples/invalid-signature.yaml --target http://127.0.0.1:8000/webhook/signature
~~~

The invalid-signature scenario expects 401, so a correct rejection is reported as a passing test.

## Write your own scenario

~~~yaml
version: 1
name: Order webhook idempotency
delivery:
  concurrency: 4
  duplicate_count: 2
  order: in_order
  timeout_ms: 1500
  signature: valid
retry:
  max_attempts: 3
  backoff_ms: 200
  retry_on: [408, 429, 500, 502, 503, 504]
events:
  - id: order.created
    event_id: evt_order_1001
    payload:
      order_id: order_1001
      total: 12500
    expect_status: [200]
    expect_json: { duplicate: false }
    expect_duplicate_json: { duplicate: true }
~~~

See docs/scenario-format.md for the complete schema.

## Reports

~~~bash
hookstorm run examples/payment-retry.yaml --target http://127.0.0.1:8000/webhook/flaky --format html --output artifacts/payment-retry.html
hookstorm run examples/payment-retry.yaml --target http://127.0.0.1:8000/webhook/flaky --format json
~~~

Reports include event IDs, delivery/attempt counts, status codes, latency, and pass/fail checks. They intentionally omit request payloads, request headers, response bodies, and secrets.

## Safety and privacy

- **Local-only by default.** Targets must resolve to loopback. A remote URL requires the explicit --allow-remote switch.
- Only send tests to systems you own or are authorized to test. Do not point chaos scenarios at a production payment endpoint.
- Redirects are not followed. Credentials embedded in target URLs are rejected.
- HMAC secrets are read from HOOKSTORM_SECRET (or a custom --secret-env variable), not from the scenario file.
- No cloud relay, tunnel, telemetry, account, or hosted service is involved.
- Reports omit payloads, headers, response bodies, secrets, and URL query strings. Event IDs and endpoint paths remain; review reports before sharing them.
- The demo receiver is for development only, not public exposure.

See SECURITY.md and docs/architecture.md for additional details.

## Development

~~~bash
python -m pip install -e '.[dev]'
ruff check .
pytest
~~~

CI tests Python 3.11, 3.12, and 3.13 on Linux, plus Python 3.12 on Windows and macOS. Every job runs lint, tests, and a package build.

## Roadmap

- [x] YAML scenario format and validation
- [x] Local-first CLI with safety guard
- [x] Retries, duplicates, reverse ordering, delays and timeouts
- [x] HMAC signature modes and response assertions
- [x] HTML / JSON reports
- [x] Local demo receiver and CI test suite
- [x] Bounded concurrent event delivery groups
- [ ] Per-event scheduling
- [ ] Export scenarios as pytest tests
- [ ] Interactive run timeline / optional local dashboard
- [ ] More provider-style signature adapters

## Contributing

Bug reports, reproducible scenarios, documentation improvements, and focused pull requests are welcome. Read CONTRIBUTING.md before opening a PR.

## License

MIT. See LICENSE.
