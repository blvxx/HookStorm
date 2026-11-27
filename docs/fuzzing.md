# Seeded payload fuzzing

`hookstorm fuzz` applies deterministic, structure-aware mutations to the JSON payloads in a validated version 1 scenario and sends each mutated scenario to the configured test endpoint.

## Example

~~~bash
hookstorm fuzz scenarios/webhook.yaml \
  --target http://127.0.0.1:8000/webhook \
  --seed 42 \
  --iterations 25 \
  --minimize-budget 20 \
  --cases-dir .hookstorm/fuzz-cases \
  --output .hookstorm/fuzz-summary.json
~~~

The command uses the same local-only target policy as `hookstorm run`. Remote HTTPS requires `--allow-remote`; non-loopback plaintext HTTP additionally requires `--allow-insecure-http`. Use isolated development or staging systems that you own or are authorized to test. Do not fuzz production payment, identity, or critical infrastructure endpoints.

For signed scenarios, set the environment variable referenced by `--secret-env` (default: `HOOKSTORM_SECRET`). Provider profiles can be selected with `--provider`, or declared in the YAML file.

## Reproducibility

- The default seed is `1`; use `--seed` to select and reproduce a mutation sequence.
- Every trial receives a distinct deterministic event ID. Provider-managed delivery metadata is regenerated for the new ID.
- `--iterations` accepts 1–500 mutations. Runs are sequential and do not persist to the normal SQLite history.
- The worst-case estimate, including retry/duplicate attempts and the requested minimization budget, is capped at 50,000 HTTP attempts per command.
- Mutations include object-key insertion/removal, array item insertion/removal, scalar type changes, and boundary-like scalar values. Payloads remain valid JSON and stay within the 1 MiB scenario limit.

The JSON summary records the seed, scenario fingerprint for each trial, event ID, mutation path and operation, execution result, and aggregate counts. It does not copy request payloads, headers, response bodies, or secret values.

## Failure minimization and replay

Set `--minimize-budget N` (0–100) to allow up to N additional executions for each failing case. The reducer greedily tries smaller objects, arrays, and scalar values, and keeps a reduction only when the same scenario failure is still observed. The result is bounded and reproducible, but it is not guaranteed to be the globally smallest payload.

Use `--cases-dir` to save replayable YAML for failures and minimized cases. Without this option, payloads are not written to disk. Profile-managed headers are removed from saved YAML and regenerated when the case is loaded again.

Replay a saved failure with the regular runner:

~~~bash
hookstorm run .hookstorm/fuzz-cases/fuzz-seed-42-case-0001-minimized.yaml \
  --target http://127.0.0.1:8000/webhook
~~~

Provide the same signing secret and remote-target opt-ins used during fuzzing, if required. A saved scenario contains the payload needed for reproduction, so protect the cases directory as test data.

## Exit status and limits

- `0`: all generated scenarios passed their configured assertions.
- `1`: one or more fuzzed scenarios failed their configured assertions.
- `2`: invalid scenario, target policy, secret configuration, or other command error.

This first fuzzer mutates JSON payloads only. It does not create malformed HTTP framing, invalid UTF-8, compressed or chunked transfer edge cases, partial socket writes, or arbitrary network faults. Those are separate transport-level test features.
