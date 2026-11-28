# HookStorm

[English](README.md) | [Русский](README.ru.md)

**Test webhook reliability before production does.**

HookStorm is a local-first CLI for testing webhook receivers under controlled failure conditions. It sends real HTTP requests to an endpoint you choose, exercises retries and delivery edge cases, and checks the receiver's responses and (optionally) its resulting business state.

[![CI](https://github.com/blvxx/HookStorm/actions/workflows/ci.yml/badge.svg)](https://github.com/blvxx/HookStorm/actions/workflows/ci.yml)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-3776AB)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

> **Status: alpha (0.1.0a1).** The package has not yet been published to PyPI. Until the first release, install HookStorm from this repository.

## Why HookStorm?

A webhook integration can pass a happy-path test and still fail when:

- a provider retries after a timeout and the same payment is processed twice;
- duplicate or out-of-order events corrupt application state;
- a receiver rejects a valid signature or accepts an invalid one;
- a slow response exceeds the sender's deadline;
- the receiver commits a database change but the HTTP acknowledgement is lost.

HookStorm helps reproduce these cases with repeatable YAML scenarios. It is a **test tool**, not a webhook relay, inbox, queue, or production delivery service.

## What it can test

| Area | Capabilities |
|---|---|
| Delivery behavior | Retries, exponential backoff, duplicate events, reverse ordering, bounded concurrency (up to 50 events), scheduled event offsets |
| Failure simulation | Per-attempt connection errors before send; simulated acknowledgement loss after the receiver has processed a request |
| Signatures | Generic HMAC, Stripe, GitHub, Shopify, and Standard Webhooks v1 |
| Assertions | Expected HTTP status, nested JSON subset matching, separate expectations for duplicate deliveries |
| Business state | Optional read-only GET probe after all deliveries, with optional environment-backed Bearer or API-key authentication |
| Diagnostics | Preflight checks for scenario, endpoint, signing secret, verification credentials, and optional DNS/TCP/TLS connectivity |
| Reports | Self-contained HTML, JSON, and JUnit XML |
| History | Local SQLite run history, sanitized JSON baselines, and regression comparisons for failures, coverage, and latency |

Each retry and duplicate delivery reuses the event ID and request body. Timestamped signatures are regenerated for each attempt. JSON and JUnit reports preserve scenario order even when the network execution order is reversed or concurrent.

## Quick start

**Requirements:** Python 3.11 or newer.

### 1. Install from source

```bash
git clone https://github.com/blvxx/HookStorm.git
cd HookStorm

python -m venv .venv

# Linux / macOS
source .venv/bin/activate

# Windows PowerShell
# .\.venv\Scripts\Activate.ps1

python -m pip install .
```

For development and tests, install the optional tools as well:

```bash
python -m pip install -e ".[dev]"
ruff check .
pytest
```

### 2. Start the demo receiver

In a second terminal, from the repository directory:

```bash
python examples/receiver.py
```

The receiver listens on `127.0.0.1:8000` and provides endpoints for retries, slow responses, signatures, and acknowledgement-loss demonstrations. It uses only the Python standard library.

### 3. Run your first scenario

Back in the HookStorm terminal:

```bash
hookstorm --version
hookstorm validate examples/payment-retry.yaml
hookstorm run examples/payment-retry.yaml \
  --target http://127.0.0.1:8000/webhook/flaky
```

The demo endpoint returns HTTP 503 once, then accepts the retry. HookStorm writes an HTML report to `hookstorm-report.html` by default.

## A scenario is just YAML

This small example sends an order event, retries selected HTTP statuses, and checks the response:

```yaml
version: 1
name: Order webhook
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
    expect_json:
      accepted: true
```

Validate it without sending traffic, then run it:

```bash
hookstorm validate scenarios/order.yaml
hookstorm run scenarios/order.yaml --target http://127.0.0.1:8000/webhook
```

Scenario files support one to 500 events. Each payload is limited to 1 MiB. Unknown fields and duplicate YAML keys are rejected so configuration mistakes fail early.

See [Scenario format](docs/scenario-format.md) for every supported field and limit.

## Important capabilities

### Retries, duplicates, and order

- `retry.max_attempts` is the total number of attempts, including the first request (1–10).
- `retry.retry_on` controls which HTTP status codes trigger another attempt. Timeouts and transport errors are retried independently while attempts remain.
- `delivery.duplicate_count` adds repeat deliveries with the same event ID and body (0–100).
- `delivery.order: reverse` reverses event execution order.
- `delivery.concurrency` runs independent events concurrently (1–50); retries and duplicates for one event remain sequential.
- `events[].schedule_after_ms` schedules an event relative to the start of the run.

### Deterministic failure injection

`delivery.fault_injection` can select attempts for two client-side simulations:

- `connect_error_attempts`: fail before the request reaches the receiver;
- `response_loss_attempts`: let the receiver process the request, then discard its response before HookStorm observes the acknowledgement.

These are controlled test simulations, not a network proxy. To test a real receiver-side TCP close after a database commit, run `examples/commit-then-drop.yaml` against the demo receiver's `/webhook/commit-then-drop` endpoint.

See [Ambiguous webhook acknowledgements](docs/ambiguous-acknowledgements.md).

### Signature testing

Supported providers:

| Provider | Signature header |
|---|---|
| Generic | `X-HookStorm-Signature` |
| Stripe | `Stripe-Signature` |
| GitHub | `X-Hub-Signature-256` |
| Shopify | `X-Shopify-Hmac-Sha256` |
| Standard Webhooks v1 | `webhook-id`, `webhook-timestamp`, `webhook-signature` |

For `signature: valid` or `signature: invalid`, set `HOOKSTORM_SECRET` in the shell. Standard Webhooks secrets must use the `whsec_` prefix. Do not put signing secrets in YAML or commit them to source control.

The Standard Webhooks implementation supports v1 HMAC signing; asymmetric signing and key rotation are not currently implemented.

### Verify business state after delivery

An HTTP 200 response does not prove that an order or payment was written only once. Add an optional `verify` section to call a read-only GET endpoint after all deliveries:

```yaml
verify:
  url: http://127.0.0.1:8000/__hookstorm/state
  expect_status: [200]
  expect_json:
    processed_events: 1
```

HookStorm sends an `X-HookStorm-Run` header so the receiver can return state for the current run. Verification can use environment-backed Bearer or API-key authentication. The probe is read-only from HookStorm's perspective; your endpoint is responsible for exposing state without mutating it. This is an assertion about the state your endpoint exposes, not a universal exactly-once guarantee.

## Useful commands

| Command | Purpose |
|---|---|
| `hookstorm init` | Generate a starter scenario and a GitHub Actions workflow |
| `hookstorm init --no-workflow` | Generate only a scenario |
| `hookstorm validate FILE.yaml` | Validate YAML without sending requests |
| `hookstorm doctor FILE.yaml` | Check configuration and required environment variables |
| `hookstorm doctor FILE.yaml --connect` | Also check DNS/TCP and TLS connectivity; sends no HTTP request |
| `hookstorm run FILE.yaml --target URL` | Execute the scenario |
| `hookstorm export-pytest FILE.yaml` | Generate a pytest integration test |
| `hookstorm history list` | List recent local runs |
| `hookstorm history show RUN_ID` | Inspect one stored run |
| `hookstorm history compare BASELINE_RUN_ID` | Compare the latest run with a baseline |
| `hookstorm history export latest --output baseline.json` | Export a portable sanitized baseline |

Run exit codes: `0` means assertions passed, `1` means the run completed but an assertion failed, and `2` means a configuration or execution error occurred.

## Reports and history

Choose a report format with `--format html|json|junit`. HTML is the default. JUnit produces a testcase for each delivery, including duplicates, so it can be consumed by CI systems.

Run history is stored locally in SQLite by default:

- Linux / macOS: `~/.hookstorm/history.sqlite3`
- Windows: `%USERPROFILE%\\.hookstorm\\history.sqlite3`

History and exported baselines omit payloads, request headers, response bodies, and secret values. They retain operational identifiers such as event IDs and the target URL without its query string; review these files before sharing them. On POSIX systems, the default history directory and files are restricted to the current user.

Portable JSON baselines let separate CI jobs compare runs without sharing a SQLite database. See [CI integration](docs/ci-integration.md).

## Safety by default

- **Loopback only by default.** `hookstorm run` blocks remote targets unless explicitly opted in.
- Remote HTTPS requires `--allow-remote`.
- Non-loopback HTTP requires both `--allow-remote --allow-insecure-http`. Prefer HTTPS.
- Redirects are not followed, embedded URL credentials are rejected, and HTTPX environment proxy settings are not trusted.
- Response bodies are streamed with a 2 MiB limit. HookStorm requests identity encoding and rejects unexpected compression before decompression.
- Reports do not include payloads, request headers, response bodies, or secrets, but they do contain target information and event identifiers.
- Use isolated development or staging data. Do not run chaos scenarios against production payment or critical infrastructure endpoints.

See [Security policy](SECURITY.md) and [Architecture](docs/architecture.md).

## Integrate with GitHub Actions

Run `hookstorm init` inside your application repository to generate:

- `scenarios/webhook.yaml` — the scenario to adapt to your receiver;
- `.github/workflows/webhook-regression.yml` — a copy-ready staging workflow.

Configure the GitHub `staging` Environment with `HOOKSTORM_TARGET_URL` as a variable and `HOOKSTORM_SECRET` as a secret when signatures are used. The generated workflow skips fork-originated pull requests, runs a preflight check before sending events, and refreshes its baseline only on successful trusted runs on `main`.

See [CI integration for application repositories](docs/ci-integration.md) for setup details, baseline behavior, and environment protection.

## Limits and known boundaries

- Maximum 500 events per scenario; payload limit 1 MiB per event.
- Maximum 50 concurrent independent events; up to 100 additional duplicates per event.
- Per-request timeout: up to 5 minutes. Whole-run budget: 60 minutes by default, configurable up to 24 hours.
- Response body limit: 2 MiB; compressed responses are rejected.
- Client-side response-loss injection is not a real network drop. Use the receiver-side commit-then-drop fixture when you need to test a real TCP close.
- Standard Webhooks support is v1 HMAC only.

## Documentation

- [Scenario format](docs/scenario-format.md)
- [Architecture](docs/architecture.md)
- [Ambiguous acknowledgements and idempotency](docs/ambiguous-acknowledgements.md)
- [GitHub Actions integration](docs/ci-integration.md)
- [Changelog](CHANGELOG.md)
- [Security policy](SECURITY.md)
- [Contributing](CONTRIBUTING.md)
- [Release checklist](docs/release-checklist.md)

## Development

```bash
python -m pip install -e ".[dev]"
ruff check .
pytest
python -m build --sdist --wheel --outdir dist
python scripts/smoke_wheel.py --all
```

CI tests Python 3.11–3.14 on Ubuntu, plus Python 3.12 on Windows and macOS. Each matrix job runs lint and tests, builds a wheel and source distribution, and smoke-tests both installed artifacts.

## License

MIT. See [LICENSE](LICENSE).
