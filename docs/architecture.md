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
- runner.py: sends requests asynchronously with HTTPX, preserves event IDs, supports bounded concurrency across independent events, computes optional HMAC signatures, and checks nested JSON responses.
- report.py: generates JSON or self-contained HTML. Payloads, request headers, response bodies, and secrets are not stored.
- examples/receiver.py: standard-library local demo server; never use it as a public endpoint.

## Deliberate trade-offs

1. CLI before dashboard. The scenario runner and deterministic reports are the core value.
2. No hosted receiver. Users point HookStorm at their own test endpoint.
3. No implicit remote access. Loopback is the default; remote testing requires an explicit opt-in.
4. No raw payloads in reports. Reports focus on delivery behavior instead of copying business data.

## Current limitations

- Concurrency applies across independent events; retries and duplicate deliveries within one event remain sequential. With concurrency above 1, request completion order is intentionally nondeterministic.
- A timeout does not prove the receiver rolled back work. The handler may finish after the sender stops waiting.
- HMAC mode is a generic raw-body SHA-256 convention, not a drop-in for every provider.
- Response assertions inspect expected top-level JSON fields only.
- A passing status or JSON assertion does not prove the business side effect happened exactly once; verify it independently in your test environment.
