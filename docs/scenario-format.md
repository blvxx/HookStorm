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

Unknown keys are rejected to catch typos early.

## Delivery settings

| Field | Default | Constraints / behavior |
|---|---|---|
| order | in_order | in_order or reverse; reverse reverses the event list. |
| duplicate_count | 0 | Additional deliveries per event, 0–100. Reuses the same event ID and body. |
| delay_ms | 0 | Delay before each request, 0–60,000 ms. |
| timeout_ms | 5000 | HTTP request timeout, 1–300,000 ms. |
| signature | none | none, valid, invalid, or missing. |

Delivery settings may be global or defined per event. Per-event settings override global settings.

For signature valid or invalid, set HOOKSTORM_SECRET in the shell. The signature is sha256=<hex HMAC-SHA256> over the compact JSON request body and is sent as X-HookStorm-Signature.

## Retry settings

| Field | Default | Constraints / behavior |
|---|---|---|
| max_attempts | 1 | Total tries per delivery, 1–10 (including the first request). |
| backoff_ms | 0 | Base exponential backoff, 0–60,000 ms; each actual wait is capped at 60 seconds. |
| retry_on | [408, 429, 500, 502, 503, 504] | HTTP statuses that trigger another attempt. Timeouts and transport errors are retried while attempts remain. |

Backoff grows exponentially as backoff_ms * 2^(attempt - 1), capped at 60 seconds per wait. Retries preserve the event ID and request body. HookStorm rejects scenarios whose worst-case delivery attempts exceed 20,000.

## Event fields

| Field | Required | Meaning |
|---|---:|---|
| id | yes | Event type, for example invoice.paid (1–256 characters, no line breaks). |
| event_id | yes | Stable event identifier reused across retries and duplicates (1–256 characters, no line breaks). |
| payload | no | JSON-compatible value; defaults to {}. |
| headers | no | Additional string headers (up to 64 headers / 16 KiB total). Names and values cannot contain line breaks. Transport-managed headers cannot be overridden. |
| delivery | no | Per-event delivery overrides. |
| expect_status | no | Accepted final HTTP status code or list; defaults to [200, 201, 202, 204]. |
| expect_json | no | Expected top-level JSON fields for the first delivery. |
| expect_duplicate_json | no | Expected top-level JSON fields for additional deliveries. |

JSON expectations are subset matches: additional response keys are allowed. The final attempt of each delivery is checked. A delivery passes only when its final status and configured JSON expectation match.

## Built-in request headers

HookStorm sends Content-Type, User-Agent, X-HookStorm-Run, X-Event-ID, X-Event-Type, X-HookStorm-Attempt, and X-HookStorm-Duplicate-Index. Custom headers may not override transport-managed headers or Host, Content-Length, and Transfer-Encoding.
