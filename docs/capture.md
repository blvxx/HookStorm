# Webhook capture

HookStorm can record incoming JSON webhook requests and turn them into a version 1 scenario.

## Start a local capture receiver

~~~bash
hookstorm capture --output scenarios/captured-webhooks.yaml
~~~

The receiver listens on **127.0.0.1:8787 only**. It does not expose a public URL, create a tunnel, or accept a configurable non-loopback bind address. Send one or more webhook requests to the printed local URL, then press Ctrl+C. The scenario is updated atomically after every accepted request, so captured events are retained even if the process is interrupted.

For a free local port:

~~~bash
hookstorm capture --port 0 --output scenarios/capture.yaml
~~~

The chosen port is printed at startup. By default capture stops accepting additional events after 25 valid requests; adjust this with `--max-events` (1–500).

## Safety and data handling

- Only HTTP POST requests with one valid `Content-Length` header are accepted. Chunked request bodies are rejected.
- Bodies must be UTF-8 JSON. Non-JSON bodies receive HTTP 400 because scenario version 1 models JSON payloads; raw binary or form bodies cannot be replayed faithfully by this command.
- Each request body is limited to 1 MiB; the complete captured scenario is limited to 8 MiB.
- Authorization, cookies, API keys, tokens, password-like fields, and signature headers are omitted by default.
- The receiver is strictly bound to IPv4 loopback. It does not use redirects or forward captured requests anywhere.
- The scenario file is atomically replaced after each accepted event and uses POSIX permissions `0600`.

To retain sensitive headers for offline forensic review, explicitly pass:

~~~bash
hookstorm capture --include-sensitive-headers --output private-capture.yaml
~~~

Sensitive header values are stored under `capture.events[].sensitive_headers` metadata, not under the event's replayed `headers` mapping. The runner ignores this metadata and the metadata is excluded from the scenario fingerprint. These values are **not sent again** by `hookstorm run`; configure a provider profile and a secret environment variable to generate a fresh signature instead. Treat the resulting file as a secret and do not commit or share it without reviewing and redacting the values.

## Replay and edit

Validate the generated scenario:

~~~bash
hookstorm validate scenarios/captured-webhooks.yaml
~~~

Then point it at your own local or isolated staging receiver:

~~~bash
hookstorm run scenarios/captured-webhooks.yaml --target http://127.0.0.1:8000/webhook
~~~

Captured event IDs are replaced with unique local IDs so the same event can be captured more than once without violating scenario version 1 uniqueness. The original upstream event ID, when available, is kept as capture metadata and does not change replay behavior.

Capture stores payloads and non-sensitive custom headers. Review the scenario before sharing it: payloads and event paths can still contain personal or business data.
