# Changelog

## 0.1.0 — Initial public development release

- Add YAML scenario validation and CLI commands (run, validate).
- Add retries, duplicate delivery, reverse event ordering, delays, timeouts, and HMAC modes.
- Add HTTP status and JSON response assertions.
- Add JSON and self-contained HTML reports.
- Block non-loopback targets by default.
- Add local demo receiver, sample scenarios, tests, and GitHub Actions CI.


## Unreleased

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
