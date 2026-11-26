# Scenario format (version 1)

A scenario is a YAML document. It describes events, delivery behavior, and expected receiver responses.

## Top-level fields

| Field | Required | Meaning |
|---|---:|---|
| version | yes | Must be 1. |
| name | yes | Human-readable scenario name. |
| description | no | Optional explanation. |
| delivery | no | Default delivery fault settings. |
| retry | no | Retry policy; defaults to one attempt. |
| events | yes | One to 500 event definitions. Payloads are limited to 1 MiB each. |
| verify | no | Optional read-only GET probe for checking backend state after all deliveries. |

Unknown keys are rejected to catch typos early. Scenarios can be exported with `hookstorm export-pytest`; the generated test embeds the normalized scenario and reads its endpoint from `HOOKSTORM_TARGET` at runtime.

## Delivery settings

| Field | Default | Constraints / behavior |
|---|---|---|
| order | in_order | in_order or reverse; reverse reverses the event list. |
| concurrency | 1 | Maximum number of independent events sent concurrently; 1–50. |
| duplicate_count | 0 | Additional deliveries per event, 0–100. Reuses the same event ID and body. |
| delay_ms | 0 | Delay before each request, 0–60,000 ms. |
| timeout_ms | 5000 | HTTP request timeout, 1–300,000 ms. |
| signature | none | none, valid, invalid, or missing. |
| signature_provider | generic | generic, stripe, github, shopify, or standard_webhooks. |
| signature_timestamp_offset_seconds | 0 | Stripe and Standard Webhooks; shifts the signed attempt timestamp by -86,400 to +86,400 seconds for replay-window tests. |

Delivery settings may be global or defined per event. Per-event settings override global settings. The `concurrency` setting is global only. Retries and duplicate deliveries belonging to one event stay sequential; independent events may overlap. The report keeps events in scenario order even when requests finish out of order.

For signature valid or invalid, set `HOOKSTORM_SECRET` in the shell. HookStorm signs the exact compact JSON request bytes sent over HTTP. The default `generic` provider preserves the legacy `X-HookStorm-Signature: sha256=<hex>` format.

Provider formats:

| Provider | Headers | Signed content / encoding |
|---|---|---|
| [`stripe`](https://docs.stripe.com/webhooks/signature) | `Stripe-Signature` | HMAC-SHA256 over `<unix_timestamp>.<raw_body>`; header contains `t=<timestamp>,v1=<hex>`. |
| [`github`](https://docs.github.com/en/webhooks/using-webhooks/validating-webhook-deliveries) | `X-Hub-Signature-256` | HMAC-SHA256 over the raw body, hex encoded with the `sha256=` prefix. |
| [`shopify`](https://shopify.dev/docs/apps/build/webhooks/verify-deliveries) | `X-Shopify-Hmac-Sha256` | HMAC-SHA256 over the raw body, Base64 encoded. |
| [`standard_webhooks`](https://www.standardwebhooks.com/) | `webhook-id`, `webhook-timestamp`, `webhook-signature` | HMAC-SHA256 over `<webhook-id>.<unix_timestamp>.<raw_body>`; signature is `v1,<base64>`. |

For `standard_webhooks`, `HOOKSTORM_SECRET` must use the Standard Webhooks `whsec_` prefix followed by Base64-encoded key material. The decoded HMAC key must be 24–64 bytes. HookStorm sends the scenario's event ID as `webhook-id`, keeps it stable across retries, and generates a fresh `webhook-timestamp` and signature for every delivery attempt. A single v1 signature is generated per attempt; key rotation and asymmetric `v1a` signing are not currently implemented.

`signature: invalid` changes the digest while keeping the provider's header shape. `signature: missing` omits the provider signature headers. Timestamp offsets let you test receivers that enforce timestamp tolerance; the demo receiver rejects signed timestamps more than 300 seconds from its current clock.

## Retry settings

| Field | Default | Constraints / behavior |
|---|---|---|
| max_attempts | 1 | Total tries per delivery, 1–10 (including the first request). |
| backoff_ms | 0 | Base exponential backoff, 0–60,000 ms; each actual wait is capped at 60 seconds. |
| retry_on | [408, 429, 500, 502, 503, 504] | HTTP statuses that trigger another attempt. Timeouts and transport errors are retried while attempts remain. |

Backoff grows exponentially as backoff_ms * 2^(attempt - 1), capped at 60 seconds per wait. Retries preserve the event ID and request body. For timestamped signing formats (Stripe and Standard Webhooks), HookStorm regenerates the timestamp and signature for each attempt. HookStorm rejects scenarios whose worst-case delivery attempts exceed 20,000.

## Business-state verification

The optional top-level verify mapping runs once after all event deliveries have completed. It is intended for a read-only test endpoint that can expose the state produced by the current run.

| Field | Default | Constraints / behavior |
|---|---|---|
| url | required | Absolute HTTP(S) URL. Localhost / loopback only unless the CLI uses --allow-remote. Embedded credentials and fragments are rejected. |
| expect_status | [200] | Accepted HTTP status code or non-empty list of codes from 100 to 599. |
| expect_json | required | JSON object checked with the same recursive subset-matching rules as event response assertions. |
| timeout_ms | 5000 | GET timeout, 1–300,000 ms. |
| auth | none | Optional environment-backed Bearer or API-key header authentication. |

HookStorm sends the generated X-HookStorm-Run header with the GET so a receiver can isolate state for that run. The probe sends no webhook payload and does not mutate the receiver. A status, transport, or JSON assertion failure marks the overall run as failed. JSON and HTML reports include the probe result; JUnit adds a separate business-state probe testcase. Run history stores the sanitized outcome and compares it for regressions.

Authentication is optional and configured with an environment variable reference, not a literal secret:

~~~yaml
auth:
  type: bearer
  token_env: HOOKSTORM_VERIFY_TOKEN
~~~

For API-key authentication, use:

~~~yaml
auth:
  type: api_key
  key_env: HOOKSTORM_VERIFY_API_KEY
  header: X-API-Key
~~~

The `header` field is optional and defaults to `X-API-Key`. A custom API-key header must be a valid HTTP header name and may not replace Authorization, HookStorm-managed headers, or hop-by-hop headers. Bearer tokens are sent as `Authorization: Bearer <token>`.

The named environment variable is resolved before any webhook delivery starts. Missing, empty, oversized, non-ASCII, or whitespace-containing credentials fail preflight without sending webhook traffic. Credential values are sent only on the verification GET and are never copied to terminal output, reports, or run history. Authenticated remote verification URLs must use HTTPS; plain HTTP is allowed only for localhost or loopback development endpoints. Store the variable in your CI secret manager or local shell environment; do not put the value in YAML.

The probe is not a universal exactly-once guarantee: it verifies only the state your endpoint exposes at the time of the GET. Use isolated test data and an endpoint designed for testing.

## Event fields

| Field | Required | Meaning |
|---|---:|---|
| id | yes | Event type, for example invoice.paid (1–256 characters, no line breaks). |
| event_id | yes | Stable event identifier reused across retries and duplicates (1–256 characters, no line breaks). |
| payload | no | JSON-compatible value; defaults to {}. |
| headers | no | Additional string headers (up to 64 headers / 16 KiB total). Names and values cannot contain line breaks. Transport-managed headers cannot be overridden. |
| delivery | no | Per-event delivery overrides. |
| expect_status | no | Accepted final HTTP status code or list; defaults to [200, 201, 202, 204]. |
| expect_json | no | Expected JSON object for the first delivery; nested objects are subset-matched recursively. |
| expect_duplicate_json | no | Expected JSON object for additional deliveries; nested objects are subset-matched recursively. |

JSON object expectations are recursive subset matches: additional object keys are allowed at every depth. Arrays must match in length, order, and value. Boolean values are distinct from numbers. The final attempt of each delivery is checked. A delivery passes only when its final status and configured JSON expectation match.

## Built-in request headers

HookStorm sends Content-Type, User-Agent, X-HookStorm-Run, X-Event-ID, X-Event-Type, X-HookStorm-Attempt, and X-HookStorm-Duplicate-Index. Custom headers may not override transport-managed headers or Host, Content-Length, and Transfer-Encoding.


## CI reports

Use `hookstorm run ... --format junit --output test-results/scenario.xml` to export each delivery as a JUnit `testcase`. Duplicate deliveries are separate testcases. HTTP status or JSON assertion mismatches are reported as `failure` elements; timeouts and transport errors without an HTTP response are reported as `error` elements. The report includes event type, event ID, duplicate index, attempt count in failure details, and request latency. It does not include payloads, headers, response bodies, secrets, or URL query strings.

The repository's `Webhook scenario tests` GitHub Actions workflow starts the local demo receiver, executes provider-signature examples (including Standard Webhooks), retry and duplicate-delivery cases, a commit-then-drop ambiguous-acknowledgement case, business-state verification, and out-of-order delivery, then uploads the XML files as a workflow artifact. The Standard Webhooks suite includes stale-timestamp rejection against the demo receiver's five-minute freshness window.

For use in another application repository, start from `examples/ci/webhook-regression.yaml` and adapt it to the application's staging webhook contract. The companion `examples/ci/webhook-regression.yml` is a copy-ready GitHub Actions workflow; see [CI integration for application repositories](ci-integration.md) for environment variables, secrets, baseline initialization, and fork-PR behavior.
