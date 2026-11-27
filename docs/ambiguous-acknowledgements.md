# Ambiguous webhook acknowledgements

## Why this failure matters

A webhook receiver can commit a business operation and still fail to deliver its HTTP response to the sender. The connection may reset, a reverse proxy may time out, or the process may exit after the database transaction commits. From the sender's perspective the outcome is ambiguous: it cannot distinguish “the operation did not happen” from “the operation happened but the acknowledgement was lost”.

Reliable webhook providers commonly retry these ambiguous deliveries. A receiver therefore needs idempotent processing, not just a successful first response.

## Reproduce it locally

Start the built-in demo receiver:

~~~bash
python examples/receiver.py
~~~

It listens only on `127.0.0.1:8000`. In another terminal, run:

~~~bash
hookstorm validate examples/commit-then-drop.yaml

hookstorm run examples/commit-then-drop.yaml \
  --target http://127.0.0.1:8000/webhook/commit-then-drop
~~~

The `/webhook/commit-then-drop` route intentionally does the following for the first request with a given run ID and event ID:

1. Inserts the business event into SQLite using a unique `(run_id, event_id)` key.
2. Commits the transaction.
3. Closes the TCP connection without sending an HTTP response.

HookStorm sees a transport error and retries the exact same event ID and body. The second request receives a successful response with `duplicate: true` and `effect_applied: false`. Finally, the read-only `verify` GET checks that the current run created exactly one business row.

The expected report is a passing run with two attempts for one delivery and a passing business-state verification.

## Apply the pattern to your application

Do not point this demo at production. For an application-specific test:

- Use an isolated local database or staging tenant and synthetic records.
- Persist an idempotency record keyed by the provider's stable event ID, ideally with a database uniqueness constraint.
- Commit the idempotency record and business effect atomically. Avoid a separate “check then insert” sequence that can race under concurrent delivery.
- Return a duplicate response without repeating the business effect when the same ID is received again.
- Expose a read-only test-only state endpoint, scoped by `X-HookStorm-Run` or another test correlation key, so HookStorm can verify the resulting state.
- Ensure test endpoints and test credentials cannot mutate production data.

A retry that returns HTTP 200 is not sufficient proof of idempotency. The final state assertion is what checks the business invariant.

## Limits

The included endpoint simulates a connection loss after a SQLite commit; it is a deterministic teaching and regression fixture, not a network proxy and not a proof of distributed transaction correctness. HookStorm also supports deterministic client-side `response_loss_attempts` for faster repeatable tests: the receiver still receives and processes the request, but the runner discards the returned response. That mode validates retry/idempotency behavior from the sender's point of view; it is deliberately distinct from the real TCP close provided by `/webhook/commit-then-drop`. It does not model every proxy, queue, process crash, or database isolation mode. Reproduce the same boundary in your own test receiver to validate your application's actual transaction and idempotency implementation.

## Reproduce deterministic sender-side fault plans

The copy-ready `examples/deterministic-fault-injection.yaml` combines two repeatable faults in one run:

- Attempt 1 injects a connection failure before the HTTP request reaches the receiver.
- Attempt 2 is delivered and processed, but HookStorm discards the response before it observes the acknowledgement.
- Attempt 3 retries the same event ID, receives the duplicate response, and the verification probe confirms that only one business effect was applied.

Run it against the local receiver:

~~~bash
hookstorm validate examples/deterministic-fault-injection.yaml
hookstorm run examples/deterministic-fault-injection.yaml \
  --target http://127.0.0.1:8000/webhook
~~~

This is deterministic client-side fault injection. It makes retry and idempotency behavior reproducible without a network proxy. The existing `/webhook/commit-then-drop` endpoint remains the option for testing a real receiver-side TCP close.
