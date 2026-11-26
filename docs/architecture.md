# Architecture

HookStorm is intentionally small and local-first.

~~~text
YAML scenario
     |
     v
config.py ---- validate schema, limits, and headers
     |
     v
safety.py ---- validate target (loopback unless explicitly opted in)
     |
     v
runner.py ---- serialize -> sign -> send -> retry -> assert response
     |
     +-------> report.py ---- JSON / self-contained HTML
     |
     v
CLI exit code (0 passed, 1 assertions failed, 2 invalid input/configuration)
~~~

## Main modules

- config.py: uses yaml.safe_load, rejects unknown keys, applies defaults, and bounds scenario sizes and timings.
- safety.py: accepts only loopback IPs and localhost by default. --allow-remote is an explicit opt-in.
- runner.py: sends requests asynchronously with HTTPX, preserves event IDs, supports bounded concurrency across independent events, generates generic / Stripe / GitHub / Shopify HMAC headers from the exact request bytes, and checks nested JSON responses.
- report.py: generates JSON or self-contained HTML with elapsed-time attempt tracks, search, result filters, and expandable attempt details. Payloads, request headers, response bodies, and secrets are not stored.
- examples/receiver.py: standard-library local demo server; never use it as a public endpoint.
- pytest_export.py: generates pytest integration modules with an embedded, normalized scenario and runtime-only target / secret environment variables.

## Deliberate trade-offs

1. CLI before dashboard. The scenario runner and deterministic reports are the core value.
2. No hosted receiver. Users point HookStorm at their own test endpoint.
3. No implicit remote access. Loopback is the default; remote testing requires an explicit opt-in.
4. No raw payloads in reports. Reports focus on delivery behavior instead of copying business data.

## Current limitations

- Concurrency applies across independent events; retries and duplicate deliveries within one event remain sequential. With concurrency above 1, request completion order is intentionally nondeterministic.
- A timeout does not prove the receiver rolled back work. The handler may finish after the sender stops waiting.
- Provider signing formats match Stripe, GitHub, and Shopify webhook headers. These are request generators for authorized test endpoints, not a replacement for each provider's SDK or a general receiver-side verification library.
- JSON object assertions support recursive subset matching; arrays are compared exactly and in order.
- A passing status or JSON assertion does not prove the business side effect happened exactly once; verify it independently in your test environment.
