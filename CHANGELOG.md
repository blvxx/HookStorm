# Changelog

## Unreleased

- Add Python 3.14 to the CI compatibility matrix and package classifiers.
- Restrict local history databases and exported baseline files to owner-only POSIX permissions.
- Smoke-test installed wheel and source distributions using only the generated scenario and the installed CLI.

## 0.1.0a1 — First public alpha preview

### Release hardening

- Reject malformed, zero, and out-of-range target URL ports before opening a connection.
- Reject duplicate YAML mapping keys instead of silently accepting the last value.
- Add a configurable scenario-wide runtime budget with worst-case preflight estimation and hard cancellation; preserve existing baseline fingerprints for the default budget.
- Require published release commits to be reachable from main before building distributions.
- Pin GitHub Actions used in CI and release workflows to reviewed full commit SHAs.
- Require a separate explicit `--allow-insecure-http` opt-in for non-loopback HTTP targets, including generated pytest and staging workflow integrations.
- Stream webhook and verification responses with a 2 MiB decoded-body cap to bound memory use on untrusted endpoints.
- Refresh generated and copy-ready staging workflow pins to the revision containing the scenario runtime deadline and URL validation hardening.

### Core alpha capabilities

- Add bounded concurrent delivery for independent events (1–50 workers; sequential by default).
- Preserve scenario order in reports when concurrent requests finish out of order.
- Add nested JSON subset assertions with strict boolean-versus-number matching.
- Validate signature configuration before sending any scenario traffic.
- Add `hookstorm export-pytest` to generate pytest integration tests from YAML scenarios.
- Add provider-compatible Stripe, GitHub, and Shopify HMAC signature headers while preserving the generic format.
- Add Stripe timestamp offsets for receiver replay-window tests.
- Add a searchable HTML delivery timeline with result filters and expandable attempt details.
- Extend the local demo receiver and add provider-specific example scenarios.
- Add JUnit XML reports with per-delivery testcases and distinct assertion-failure / transport-error elements.
- Publish pytest JUnit results from every OS / Python CI matrix job.
- Add a GitHub Actions workflow that runs local provider-signature, retry, duplicate, and out-of-order scenarios and uploads JUnit artifacts.
- Add opt-out local SQLite run history with sanitized per-delivery outcomes.
- Add `history list`, `history show`, and `history compare` CLI commands.
- Detect pass-rate, failed-delivery, delivery-coverage, per-delivery latency, and p95 latency regressions; return a CI-friendly nonzero exit code when regressions are found.
- Add versioned, sanitized JSON baseline export and `history compare-file` for comparing runs across ephemeral CI runners.
- Gate the built-in GitHub Actions webhook scenarios against cached main-branch baselines; refresh baselines only after successful main runs.
- Add a copy-ready GitHub Actions regression workflow and staging scenario for application repositories, with fork-PR secret isolation and trusted-main baseline refreshes.
- Document environment setup, remote-target safety, scenario adaptation, and baseline cache behavior for external projects.
- Add `hookstorm init` to generate a starter webhook scenario and a pinned GitHub Actions staging workflow with safe no-overwrite defaults.
- Add tests for generated workflow YAML / Bash syntax, scenario validation, custom providers, path safety, and overwrite behavior.
- Add `hookstorm doctor` to validate integration scenarios, endpoint configuration, and required signing secrets before webhook delivery.
- Add optional DNS/TCP and HTTPS TLS preflight checks that send no HTTP request or webhook event, and run them before staging CI tests.
- Add `hookstorm doctor --verbose` with DNS IPv4 / IPv6 results, per-address connection timings, TLS protocol and cipher details, and certificate expiry diagnostics.
- Add `hookstorm doctor --format json` with versioned machine-readable check results, readiness status, summaries, and CI-friendly exit codes.

- Add an optional read-only post-delivery business-state probe with run-scoped verification, report output, and history regression checks.
- Add environment-backed Bearer and API-key authentication for verification probes with preflight checks and secret-safe reporting.
- Extend `hookstorm doctor` to validate verification credentials and enforce HTTPS for authenticated remote probes without sending credentials.
- Add Standard Webhooks v1 HMAC signing with `whsec_` key validation, stable message IDs, and fresh timestamped signatures per retry.
- Add a deterministic commit-then-drop receiver failure and a scenario that verifies idempotent processing after an ambiguous acknowledgement.
- Extend `hookstorm init` and CI scenarios with Standard Webhooks support.
- Add per-event run-relative scheduling offsets without blocking available concurrency slots.
- Attach an opaque SHA-256 fingerprint of behavior-defining scenario settings to each run and portable baseline.
- Treat changed scenario configuration as a regression unless the comparison explicitly opts into `--allow-config-change`.
- Warn when comparing legacy baselines that predate scenario fingerprints.
- Reject duplicate delivery identities in history comparisons instead of silently overwriting a delivery result.
- Document how to refresh baselines after intentional scenario contract changes.
- Reject non-ASCII and control characters in webhook header values before any network request.
- Require unique event IDs within a scenario to protect idempotency and history comparisons.
- Use full UUID run identifiers to reduce the risk of history key collisions.
- Refresh the generated GitHub Actions installer pin and add a regression assertion for the pinned revision.
- Build and install-test the wheel in an isolated environment on every CI matrix job.
- Add a public release checklist covering artifact validation, GitHub Releases, and optional PyPI trusted publishing.

### Initial implementation

- Add YAML scenario validation and CLI commands (run, validate).
- Add retries, duplicate delivery, reverse event ordering, delays, timeouts, and HMAC modes.
- Add HTTP status and JSON response assertions.
- Add JSON and self-contained HTML reports.
- Block non-loopback targets by default.
- Add local demo receiver, sample scenarios, tests, and GitHub Actions CI.
