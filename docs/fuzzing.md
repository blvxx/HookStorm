# Seeded scenario fuzzing

`hookstorm fuzz` applies deterministic, structure-aware mutations to JSON payloads in a validated version 1 scenario. Optional delivery dimensions let it also vary event order, duplicate counts, per-attempt delays, and deterministic transport faults.

## Example

Payload-only fuzzing remains the default:

~~~bash
hookstorm fuzz scenarios/webhook.yaml \
  --target http://127.0.0.1:8000/webhook \
  --seed 42 \
  --iterations 25 \
  --minimize-budget 20 \
  --cases-dir .hookstorm/fuzz-cases \
  --output .hookstorm/fuzz-summary.json
~~~

Enable delivery variation explicitly:

~~~bash
hookstorm fuzz scenarios/webhook.yaml \
  --target http://127.0.0.1:8000/webhook \
  --seed 42 \
  --iterations 50 \
  --vary-order \
  --vary-duplicates --max-duplicates 3 \
  --vary-delay --max-delay-ms 750 \
  --vary-faults \
  --cases-dir .hookstorm/fuzz-cases
~~~

Each enabled dimension is selected deterministically from the seed and iteration. In each trial HookStorm mutates one payload, may reverse the scenario's global event order, may override one event's duplicate count and/or delay, and may inject one fault type on one free attempt of one event. The supported fault types are pre-send connection failure, acknowledgement loss after processing, and a truncated response body after processing.

## Safety and bounds

The command uses the same local-only target policy as `hookstorm run`. Remote HTTPS requires `--allow-remote`; non-loopback plaintext HTTP additionally requires `--allow-insecure-http`. Use isolated development or staging systems that you own or are authorized to test. Do not fuzz production payment, identity, or critical infrastructure endpoints.

For signed scenarios, set the environment variable referenced by `--secret-env` (default: `HOOKSTORM_SECRET`). Provider profiles can be selected with `--provider`, or declared in the YAML file. Every trial receives distinct deterministic event IDs; provider-managed headers and body envelopes are updated when required.

| Option | Default | Limit / behavior |
|---|---:|---|
| `--seed` | 1 | Integer seed; same seed and arguments reproduce the sequence |
| `--iterations` | 25 | 1–500 sequential trials |
| `--vary-order` | off | Select `in_order` or `reverse` for each trial |
| `--vary-duplicates` | off | Override one event's duplicate count per trial |
| `--max-duplicates` | 3 | 0–100; maximum used by `--vary-duplicates` |
| `--vary-delay` | off | Override one event's delay before every HTTP attempt |
| `--max-delay-ms` | 1000 | 0–60,000 ms; maximum used by `--vary-delay` |
| `--vary-faults` | off | Add one deterministic fault to a free attempt in one event |
| `--minimize-budget` | 0 | 0–100 extra executions per failing trial |

Each generated scenario is validated again before any network request. If an optional delivery mutation would exceed that scenario's request-count or runtime budget, HookStorm skips those delivery mutations for that trial and still runs the valid payload mutation. A single scenario is limited to 20,000 HTTP attempts; an entire fuzz command, including the worst-case minimization budget, is capped at 50,000 estimated attempts.

Runs are sequential and do not persist to the normal SQLite history. The JSON summary records the active dimensions, seed, scenario fingerprint for each trial, event ID, payload mutation path and operation, delivery mutations, execution result, and aggregate counts. It does not copy request payloads, headers, response bodies, or secret values.

## Failure minimization and replay

Set `--minimize-budget N` to allow up to N additional executions for each failing case. The reducer greedily tries smaller objects, arrays, and scalar values and keeps a reduction only when the same scenario failure is still observed. Delivery settings remain fixed while the selected payload is reduced. The result is bounded and reproducible, but it is not guaranteed to be the globally smallest payload.

Use `--cases-dir` to save replayable YAML for failures and minimized cases. Without this option, payloads are not written to disk. Profile-managed headers are removed from saved YAML and regenerated when the case is loaded again.

Replay a saved failure with the regular runner:

~~~bash
hookstorm run .hookstorm/fuzz-cases/fuzz-seed-42-case-0001-minimized.yaml \
  --target http://127.0.0.1:8000/webhook
~~~

Provide the same signing secret and remote-target opt-ins used during fuzzing, if required. A saved scenario contains the payload and delivery settings needed for reproduction, so protect the cases directory as test data.

## Scope and limitations

The fuzzer varies valid JSON payloads and version 1 delivery settings. It does not yet generate malformed HTTP framing, invalid UTF-8, compressed or chunked request bodies, partial socket uploads, inaccurate Content-Length, arbitrary TCP packet loss, or secret rotation. Those remain separate transport-level fault-injection work.
