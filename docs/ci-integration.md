# CI integration for application repositories

HookStorm can run against a staging webhook endpoint and compare each run with a baseline recorded by a successful run on your default branch. This is useful for checking webhook contract behavior and detecting delivery or latency regressions before a change is released.

The copy-ready example is in [examples/ci/webhook-regression.yml](../examples/ci/webhook-regression.yml). The matching scenario example is [examples/ci/webhook-regression.yaml](../examples/ci/webhook-regression.yaml).

## Quick setup with the CLI

After installing HookStorm in your application repository, run:

~~~bash
hookstorm init
~~~

By default, the wizard creates `scenarios/webhook.yaml` and `.github/workflows/webhook-regression.yml`. In an interactive terminal, choose the signature provider (`generic`, `stripe`, `github`, `shopify`, or `standard_webhooks`). Without an interactive terminal, the provider defaults to `generic`. To generate GitLab CI instead, use `hookstorm init --ci-provider gitlab`; this creates `.gitlab-ci.yml` at the project root.

Choose a starter payload and provider with `--template`:

~~~bash
hookstorm init --template stripe
hookstorm init --template github --no-workflow
~~~

Available templates are `generic`, `stripe`, `github`, `shopify`, and `standard-webhooks`.
They generate representative payload examples and the matching signature provider. GitHub
and Shopify templates also include typical provider-specific headers. Customize the event
fields and expected response for your receiver before using the scenario.

Customize the event contract directly from the command line:

~~~bash
hookstorm init --signature-provider github \\
  --event-type issues.opened --event-id evt_issues_opened_001
~~~

Then edit the generated scenario's payload, HTTP status expectations, and JSON assertions to match your staging receiver. The generated workflow reads the staging URL and signing secret from GitHub Environment values; it never writes them into the scenario or workflow.

The command preserves existing files and exits with an error rather than overwriting them. Use `--force` only when you intend to replace both generated files. Use `--no-workflow` to create just the scenario file. The workflow installs HookStorm from a pinned repository commit. After upgrading the installed HookStorm package, run `hookstorm update-workflow` to refresh only that pin in your generated workflow (`--path .gitlab-ci.yml` for GitLab). The command preserves your other workflow changes and refuses files that do not contain exactly one recognized HookStorm install URL.

### GitLab CI setup

The generated GitLab configuration intentionally runs only on the default branch. In **Settings → CI/CD → Variables**, configure `HOOKSTORM_TARGET_URL` and, when signed events are used, `HOOKSTORM_SECRET`. Mark secrets as **Masked** and **Protected**, and protect the default branch so protected values are available to the integration job. Optionally configure `HOOKSTORM_ALLOW_INSECURE_HTTP=1` only for an authorized staging endpoint that cannot use HTTPS.

The job serializes baseline updates, compares each successful run with the cached default-branch baseline, advances that baseline only after assertions and comparison pass, and publishes a JUnit report. Feature branches and merge-request pipelines are deliberately excluded by the generated default-branch rule; this avoids sending staging credentials or test events from untrusted branch code. If you need merge-request coverage, add a separate unprivileged validation job and design a trusted staging approval flow for integration runs.



## 1. Add a scenario

Copy the sample scenario to your application repository, for example:

```text
scenarios/webhook.yaml
```

Replace the sample event type, event ID, payload, signature provider, expected HTTP statuses, and JSON assertions with your webhook contract. Keep payloads synthetic and event IDs stable across runs so HookStorm can match the same deliveries when it compares baselines.

The example uses HookStorm's generic HMAC signature. Your test receiver must verify the same secret. You can instead select `stripe`, `github`, or `shopify` when your staging endpoint implements that provider's signature format. For Standard Webhooks, choose `standard_webhooks`; its `HOOKSTORM_SECRET` must be the `whsec_` secret supplied for that endpoint. For an unsigned test endpoint, set `signature: none`; `hookstorm doctor` detects that no signing secret is needed.

Validate the scenario locally before connecting it to CI:

```bash
hookstorm validate scenarios/webhook.yaml
```

After setting `HOOKSTORM_TARGET_URL` and (for signed scenarios) `HOOKSTORM_SECRET`, run the preflight. The same `HOOKSTORM_TARGET_URL` variable is used by `hookstorm run`, so you can omit `--target` locally unless you need to override the configured endpoint:

```bash
hookstorm doctor scenarios/webhook.yaml
hookstorm doctor scenarios/webhook.yaml --connect
```

The first command checks the scenario, target URL format, required webhook signing secret, and any verification-probe credentials. For `verify.auth`, it confirms the named environment variable is present and well-formed and rejects authenticated remote HTTP URLs; it never prints or transmits the credential. It does not verify whether the remote application accepts the token. The second command additionally resolves the webhook target host, opens a TCP connection, and verifies the TLS handshake and certificate for HTTPS. Use `--verbose` when troubleshooting to list IPv4 / IPv6 addresses, timings, negotiated TLS details, certificate subject / issuer, and expiry date:

```bash
hookstorm doctor scenarios/webhook.yaml --verbose
```

Verbose mode implies `--connect`. It does not send an HTTP request or webhook event, so it cannot validate the route, application-level authentication, or the handler's response. The command exits with code `1` when a required check fails.

For automation that needs to inspect individual checks, request structured JSON:

```bash
hookstorm doctor scenarios/webhook.yaml --format json
hookstorm doctor scenarios/webhook.yaml --verbose --format json
```

The versioned document currently uses `schema_version: 1` and includes an overall readiness result, check counts, network-check metadata, and each individual check with its details. The CLI exit code remains authoritative for shell gating: `0` means ready, `1` means a required check failed, and `2` indicates invalid command arguments. JSON is written to stdout; diagnostics do not include secret values or the complete endpoint URL.

## 2. Configure the staging environment

In your GitHub repository, open **Settings → Environments** and create an environment named `staging`.

Treat this environment as a deployment boundary, not just a place to store variables. Configure **Required reviewers** so a trusted maintainer must approve access from pull-request runs, and restrict which branches can deploy to the environment where that is practical. The workflow skips fork-originated PRs, but same-repository PRs can reach the job; do not make staging credentials available to code from contributors you do not trust. Use a dedicated staging signing secret with the minimum privileges needed, never a production credential.

Add the following values to that environment:

| Name | Type | Purpose |
|---|---|---|
| `HOOKSTORM_TARGET_URL` | Variable | Full URL of a dedicated staging/test webhook endpoint. Do not include credentials or query-string tokens. |
| `HOOKSTORM_ALLOW_INSECURE_HTTP` | Optional variable | Set to `1` only when a controlled non-loopback staging endpoint cannot use HTTPS. Defaults to `0`; authenticated remote verification probes still require HTTPS. |
| `HOOKSTORM_SECRET` | Secret | Signing secret used by signed scenarios (`signature: valid` or `invalid`). Standard Webhooks secrets must be `whsec_` followed by Base64 key material; other providers use their documented raw secret format. Unsigned scenarios do not require it. |
| `HOOKSTORM_VERIFY_TOKEN` | Optional secret | Example Bearer credential referenced by `verify.auth.token_env`; use the exact variable name from your scenario. |
| `HOOKSTORM_VERIFY_API_KEY` | Optional secret | Example API key referenced by `verify.auth.key_env`; use the exact variable name from your scenario. |
| `HOOKSTORM_SCENARIO` | Optional variable | Scenario path. Defaults to `scenarios/webhook.yaml`. |
| `HOOKSTORM_LATENCY_THRESHOLD_PCT` | Optional variable | Allowed latency increase. Defaults to `20`. |

The target must be a test environment you own or are authorized to exercise. The sample workflow passes `--allow-remote` because a staging URL is normally not loopback; remote HTTPS is the expected transport. Non-loopback plaintext HTTP is blocked by default. Only when HTTPS cannot be used for a controlled test endpoint, set the optional `HOOKSTORM_ALLOW_INSECURE_HTTP=1` environment variable; the generated workflow then passes `--allow-insecure-http` to both doctor and run. Authenticated remote verification probes still require HTTPS. Do not point it at production payment, billing, or customer-facing webhook endpoints. Use synthetic records and an isolated test account.

If the application needs to be deployed before testing, add your staging deployment step before **Preflight staging endpoint**. The workflow does not deploy your application or create the target endpoint.

## 3. Copy the workflow

Copy [examples/ci/webhook-regression.yml](../examples/ci/webhook-regression.yml) into your application repository at:

```text
.github/workflows/webhook-regression.yml
```

Review the `paths` filters and add the folders that should trigger webhook testing in your application. The example installs HookStorm from a fixed repository commit so that the test tool does not silently change between runs. Update that commit SHA when you intentionally upgrade HookStorm.

The workflow runs for pushes to `main`, pull requests targeting `main`, and manual dispatches. It intentionally skips fork-originated pull requests because those runs do not receive the staging secrets and should not send requests to a private staging endpoint. Keep your ordinary unit-test workflow enabled for fork PRs. The **Preflight staging endpoint** step runs before the first delivery and fails early on an invalid scenario, missing target or required secret, DNS/TCP failure, or TLS certificate error.

## 4. How baseline handling works

- On the first successful run on `main`, HookStorm exports the run to `.hookstorm-baselines/webhook.json`.
- GitHub Actions stores that file in a cache scoped to the application repository.
- Pull requests restore the baseline from the default branch, execute the current scenario, and run `hookstorm history compare-file`.
- A failed delivery, reduced delivery coverage, or latency increase above the configured threshold returns a non-zero exit code and fails the job.
- Pull requests never update the baseline. Only successful runs on `main` can save a refreshed baseline.
- If a PR cannot restore a baseline, the workflow fails with instructions to seed it from `main`; it does not silently disable regression checks.

The sample workflow serializes runs for each Git ref to avoid competing main-branch runs racing to update the baseline cache. Baselines are data, not secrets. GitHub cache contents can be read by people who can open a pull request against the repository, so never put tokens, credentials, or private payloads into cached files. The exported HookStorm record excludes request payloads, headers, response bodies, and signing secrets, but it retains event identifiers and target metadata. Review identifiers and assertion values before sharing reports or baselines.

GitHub documents [cache scope and security restrictions](https://docs.github.com/en/actions/reference/workflows-and-actions/dependency-caching), including that default-branch caches can be restored by other branches and must not contain secrets. Untrusted pull request workflows should use restore-only cache access; the example follows that pattern and saves cache entries only after successful main-branch runs. For general workflow hardening, see GitHub's [secure use reference](https://docs.github.com/en/actions/reference/security/secure-use), which recommends pinning actions to full commit SHAs.

## 5. Tune and troubleshoot

The default latency regression threshold is `20` percent. CI runners can be noisy, so measure several successful runs before choosing a threshold. Increase the threshold only when the observed variation is environmental noise you have investigated; keep functional delivery and coverage regressions gated.

Useful commands:

```bash
# Validate the scenario schema
hookstorm validate scenarios/webhook.yaml

# Check integration configuration (and optionally socket connectivity)
hookstorm doctor scenarios/webhook.yaml
hookstorm doctor scenarios/webhook.yaml --connect

# Run against a local test receiver
hookstorm run scenarios/webhook.yaml \
  --target http://127.0.0.1:8000/webhook \
  --history-db .hookstorm/history.sqlite3

# Export a baseline after a successful run
hookstorm history export latest --output .hookstorm-baselines/webhook.json

# Compare a later run with that baseline
hookstorm history compare-file .hookstorm-baselines/webhook.json \
  --db .hookstorm/history.sqlite3 --latency-threshold-pct 20
```

For the full scenario schema, see [scenario-format.md](scenario-format.md). For portable baseline format and CLI semantics, see the run history section in the [README](../README.md).
