# Changelog

## Unreleased

### Security

- Pin webhook and verification connections to the exact DNS address set resolved before the run, preventing connection-time re-resolution from bypassing target validation while preserving the original hostname for HTTP Host and TLS SNI.
## 0.1.0a1 — First public alpha preview (2026-11-28)

### MCP integration

- Add an optional local MCP server for sanitized run inspection, scenario introspection, and failure diagnosis.
- Add an explicitly opt-in, loopback-only replay tool that checks the failed run, scenario fingerprint, and target before sending one selected event.
- Add a dedicated Python 3.12 CI job for MCP server tools and replay safeguards.
- Add scenario-aware MCP run analysis with privacy-preserving payload/header metadata and evidence-bounded recommendations.
- Add atomic batch replay for up to 50 explicitly selected failed events, retaining loopback-only and per-call confirmation safeguards.
- Expand HTTP failure classification for conflicts, payload limits, media types, and redirects.
- Preserve the source request correlation ID during MCP replay while storing the replay as a separate history run.

### Audit fixes

- Use provider-shaped GitHub delivery GUIDs and keep Shopify delivery IDs distinct from merchant-action event IDs in generated fixtures.
- Refresh generated GitHub Actions and GitLab CI install pins to the latest reviewed main revision.

### UX and automation

- Add a self-contained `hookstorm demo` that exercises retries and duplicate delivery against a temporary loopback-only receiver.
- Allow `hookstorm run` and `watch` to use `HOOKSTORM_TARGET_URL` when `--target` is omitted.
- Add provider-oriented starter scenario templates for generic HMAC, Stripe, GitHub, Shopify, and Standard Webhooks, including typical GitHub and Shopify request headers.
- Add `hookstorm watch` to validate and rerun scenarios when their files change.
- Add `hookstorm update-workflow` to refresh only the pinned HookStorm install SHA in generated CI workflows.
- Generate an optional default-branch-only GitLab CI pipeline with serialized baseline comparison and JUnit artifacts.
- Add actionable hints for common delivery failures and document the new CLI workflows.


### Reliability and safety hardening

- Require setuptools 84.1.0 or newer for package builds, avoiding the affected range in the Windows legacy wheel-installation advisory (GHSA-xmj3-9gf7-h52w).
- Require PyYAML 6.0.4 or newer to avoid exponential YAML merge-key expansion when loading scenarios.
- Limit JSON object/array nesting to 128 levels and convert excessively nested YAML, response, and history input into controlled validation or assertion errors.

- Reject case-insensitive duplicate event request headers to avoid ambiguous HTTP field values.

- Keep the `:memory:` SQLite history database alive across save/list/show operations within one process.

- Cancel and await sibling delivery workers if one concurrent worker exits with an unexpected exception, preventing requests from outliving the run's HTTP client.

- Enforce absolute per-attempt and verification request deadlines so slow-drip responses cannot evade `timeout_ms`.
- Request identity-encoded responses and reject unexpected compression before decompression, preventing compressed responses from bypassing the 2 MiB memory limit.
- Preserve source scenario order in reports when deliveries are intentionally executed in reverse.
- Validate consistency between final delivery attempts, delivery outcomes, verification outcomes, and the overall history success flag; verify all duplicated SQLite identifiers, status flags, and metric columns against the stored JSON record.

### Delivery, diagnostics, and integration improvements

- Add a shared-clock waterfall view to the interactive HTML report, reusing search, result/fault filters, and sort controls across both timeline layouts.
- Show deterministic transport fault markers in terminal run summaries and `history show`, including the affected attempt and event.
- Expand `hookstorm init` into an interactive scenario wizard for event identity, signature provider, retries, and duplicate deliveries; expose the same options as non-interactive flags.
- Add deterministic per-attempt client-side fault injection for pre-send connection errors and post-processing acknowledgement loss; preserve markers in history and add fault-aware filtering and sorting to the interactive HTML timeline.
- Validate history run metrics and delivery identities before storage or baseline comparison; reject non-finite JSON numbers in imported baselines.
- Pin repository and generated GitHub Actions workflows to reviewed full commit SHAs, and refresh the scaffolded HookStorm installer reference to the latest reviewed revision.
- Add Python 3.14 to the CI compatibility matrix and package classifiers.
- Restrict local history databases and exported baseline files to owner-only POSIX permissions.
- Smoke-test installed wheel and source distributions using only the generated scenario and the installed CLI.
- Keep `hookstorm init` working when Windows reports the current directory through an 8.3 short-path alias.

### Release hardening

- Attach the validated wheel and source distribution to the published GitHub Release from a separate least-privilege job.

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
