# HookStorm

**Test webhook consumers before production.**

HookStorm is a local-first CLI for exercising webhook delivery, retries, duplicate events, idempotency, ambiguous acknowledgements, and recovery. It sends deterministic scenarios to a test endpoint and checks both HTTP responses and optional business state.

## Start here

- [Install HookStorm](installation.md)
- [Run your first scenario](scenario-format.md)
- [Understand provider profiles](provider-profiles.md)
- [Use HookStorm from pytest](pytest-integration.md)
- [Add a FastAPI or Flask test endpoint](framework-integrations.md)
- [Run HookStorm in GitHub Actions](github-action.md)

## What it tests

- Transient failures, retries, backoff, and request timeouts.
- Duplicate and reordered deliveries with stable event IDs.
- Lost acknowledgements and truncated response bodies after receiver processing.
- Request-side HTTP/1.1 faults, including malformed JSON, compression, chunked framing, and incomplete uploads.
- Provider-style HMAC signatures for Stripe, GitHub, Shopify, and Standard Webhooks.
- Seeded payload and delivery fuzzing with reproducible failure cases.
- HTML, JSON, and JUnit reports, plus local SQLite history and regression baselines.

HookStorm is a test runner for webhook consumers. It is not a hosted inbox, event gateway, relay, or production delivery service.

## Safety model

Loopback targets are the default. Remote HTTPS requires an explicit opt-in; non-loopback plaintext HTTP requires a second opt-in. Secrets are read from environment variables. Reports and history omit payloads, request/response headers, response bodies, and credentials.

Read the [security policy](../SECURITY.md), [architecture notes](architecture.md), and [release checklist](release-checklist.md) before using the tool against a remote environment.

## Project status

HookStorm is pre-release software. Package installation via PyPI, release tags, and hosted documentation must be verified from the corresponding public artifacts before being advertised as available.
