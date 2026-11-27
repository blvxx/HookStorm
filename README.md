# HookStorm

**A local-first CLI for testing webhook delivery failures, retries, idempotency, and recovery before production.**

[![CI](https://github.com/blvxx/HookStorm/actions/workflows/ci.yml/badge.svg)](https://github.com/blvxx/HookStorm/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.11%2B-3776AB)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

## 30 seconds to first result

Requires Python 3.11+. Run the first command in a terminal, then use two terminals from the cloned repository:

1. Clone and install:

   `git clone https://github.com/blvxx/HookStorm.git && cd HookStorm && python -m pip install .`

2. Start the local demo receiver:

   `python examples/receiver.py`

3. Run a retry scenario in another terminal:

   `hookstorm run examples/payment-retry.yaml --target http://127.0.0.1:8000/webhook/flaky`

The demo receiver binds to `127.0.0.1` only. The command writes a self-contained HTML report to `hookstorm-report.html`.

## Capabilities

| Test behavior | HookStorm support |
|---|---|
| Transient failures and retries | Bounded attempts, exponential backoff, configurable retry statuses |
| Duplicate and reordered events | Stable event IDs, duplicate deliveries, reverse ordering |
| Timing and concurrency | Request timeouts, run deadline, per-event scheduling, bounded concurrency |
| Ambiguous acknowledgements | Deterministic pre-send connection errors and post-processing response loss |
| Signatures | Generic HMAC, Stripe, GitHub, Shopify, Standard Webhooks v1 |
| Assertions | HTTP status, nested JSON, and optional read-only business-state verification |
| Fuzzing | Seeded JSON payload mutations, replayable failing cases, and bounded failure minimization |
| Reports and regression checks | HTML, JSON, JUnit, local SQLite history, portable baselines |

Scenarios use the backwards-compatible `version: 1` YAML format. Provider profiles for Stripe, GitHub, Shopify, and Standard Webhooks supply synthetic event shapes and provider headers (`hookstorm run --provider github`, or `hookstorm init --provider stripe`). See the [scenario reference](docs/scenario-format.md) and [provider guide](docs/provider-profiles.md) for fields, limits, signatures, and examples.

## How is HookStorm different?

HookStorm is a **test runner for webhook consumers**, not a hosted inbox, event gateway, relay, or production delivery service.

| Tool | Primary workflow | Where HookStorm differs |
|---|---|---|
| [webhook.site](https://webhook.site/) | Capture and inspect incoming HTTP requests at a hosted URL | HookStorm sends repeatable test events to your endpoint and checks the receiver's behavior |
| [Hookdeck](https://hookdeck.com/docs) | Managed event gateway, inspection, routing, retries, and replay | HookStorm does not sit in production traffic; it exercises your handler locally or in an explicitly opted-in test environment |
| [Stripe CLI](https://docs.stripe.com/stripe-cli) | Trigger Stripe test events and forward Stripe webhooks to a local listener | HookStorm is provider-agnostic and focuses on scenario-driven retries, duplicates, failure injection, assertions, and baselines |
| [ngrok Traffic Inspector](https://ngrok.com/docs/obs/traffic-inspection/http-requests) | Expose a local service through a tunnel, inspect traffic, and replay captured requests | HookStorm needs no public tunnel and generates controlled scenarios instead of replaying captured production/provider requests |
| [Svix Play](https://docs.svix.com/play) | Hosted webhook debugger, relay, and programmable response/failure behavior | HookStorm is local-first and adds scenario assertions plus a separate state probe for checking idempotent side effects |
| [HookChaos](https://dev.to/umutkeskin1001/hookchaos-webhook-stress-tester-cliopen-sourced-project-21ne) | Local-first chaos CLI with reusable packs, history/replay, and Git-backed sharing | The projects overlap directly; HookStorm currently emphasizes explicit per-attempt fault plans, provider HMAC formats, state verification, and baseline regression checks |

The important distinction is the **verify** probe: a `200` response is not proof that an order or payment was applied exactly once. HookStorm can make a read-only request after delivery and assert the resulting test state. Its reports and history deliberately omit request payloads, request/response headers, response bodies, and secret values.

## Safety and privacy

- Loopback targets only by default. Remote HTTPS requires `--allow-remote`; non-loopback HTTP additionally requires `--allow-insecure-http`.
- Redirects are not followed. Embedded URL credentials are rejected. The response body limit is 2 MiB after content decoding.
- Signing secrets and verification credentials are read from environment variables, never scenario literals.
- Reports and history retain the URL scheme, hostname, explicit port (when present), and path. Query strings and fragments are removed. Event identifiers and endpoint paths can still be sensitive; review artifacts before sharing them.
- History is local SQLite data. Payloads, headers, response bodies, and secret values are excluded by an explicit field whitelist.

Read [SECURITY.md](SECURITY.md) and the [architecture notes](docs/architecture.md) before testing a remote environment.

## Documentation

- [Scenario format and behavior](docs/scenario-format.md)
- [Seeded fuzzing and failure minimization](docs/fuzzing.md)
- [Provider profiles and retry-policy notes](docs/provider-profiles.md)
- [Ambiguous acknowledgements and idempotency](docs/ambiguous-acknowledgements.md)
- [Architecture and current limitations](docs/architecture.md)
- [CI integration for application repositories](docs/ci-integration.md)
- [Release and PyPI publishing checklist](docs/release-checklist.md)

## Next

Planned work, in priority order:

1. Extend seeded payload fuzzing to vary event order, duplicate deliveries, delays, and controlled failure injection.
2. A loopback-only `hookstorm capture` receiver that creates scenarios and redacts sensitive headers by default.
3. Additional request fault injection: malformed JSON, content types, compression, chunking, partial uploads, content-length mismatches, slow bodies, secret rotation, and large payloads.
4. A reusable composite GitHub Action with SHA-pinned dependencies.
5. A structured pytest integration API and small FastAPI / Flask helpers.
6. JSON Schema output for editor completion.
7. A minimal non-root Docker image and `uvx` / `pipx` installation guides.
8. A published MkDocs documentation site.
9. Provider retry schedule presets after explicit per-attempt schedule support and longer safe run-time budgets are designed.

## Development

~~~bash
python -m pip install -e '.[dev]'
ruff check .
mypy
coverage run -m pytest
coverage report
~~~

See [CONTRIBUTING.md](CONTRIBUTING.md). HookStorm is licensed under the MIT License.
