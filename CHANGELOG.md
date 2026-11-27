# Changelog

All notable changes to this project will be documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html) where practical.

## [Unreleased]

### Added

- Add `hookstorm capture`, a loopback-only JSON webhook receiver that exports version 1 scenarios, redacts sensitive headers by default, and atomically persists captures.

- Add an opt-in pytest integration API with hookstorm, hookstorm_target, and hookstorm_scenario fixtures, async execution, and a safe synchronous wrapper.
- Add a composite GitHub Action with remote-target opt-ins, optional DNS/TCP/TLS preflight, secret-env indirection, JUnit/HTML/JSON output, and history disabled by default.
- Add a dedicated GitHub Actions smoke workflow that runs the composite action against the local demo receiver.
- Add a packaged JSON Schema 2020-12 for scenario version 1 and a `hookstorm schema` export command.
- Add a YAML scenario format (version 1) and CLI commands for running and validating scenarios.
- Add retries, exponential backoff, duplicate deliveries, reverse ordering, scheduling, bounded concurrency, request timeouts, and a run-wide deadline.
- Add generic HMAC plus Stripe, GitHub, Shopify, and Standard Webhooks v1 request signatures.
- Add provider event profiles with synthetic payloads, event IDs/types, provider-managed headers, and profile-aware `hookstorm run` / `hookstorm init`.
- Add seeded JSON payload fuzzing with unique trial event IDs, replayable YAML failure cases, and an optional bounded failure reducer.
- Extend seeded fuzzing with opt-in deterministic event-order, duplicate-count, per-attempt delay, and transport-fault mutation dimensions while preserving payload-only defaults.
- Add request-level fault injection for malformed JSON, wrong Content-Type, gzip/deflate bodies, invalid compressed data, valid/malformed chunked framing, partial uploads, and Content-Length mismatch.
- Add a request-level fault matrix example and run it against the local demo receiver in webhook integration CI.
- Add deterministic pre-send connection errors, post-processing acknowledgement loss, and truncated response-body read failures, with separate idempotency examples.
- Add HTTP status and nested JSON assertions, plus an optional read-only business-state verification probe.
- Add self-contained HTML, JSON, and JUnit reports.
- Add local SQLite run history, sanitized portable JSON baselines, and regression comparison.
- Add `hookstorm init`, `hookstorm doctor`, and `hookstorm export-pytest`.
- Add local receiver and provider, retry, duplicate, ordering, scheduling, and ambiguous-acknowledgement examples.
- Add a six-environment Python / operating-system CI matrix and local webhook scenario workflow.
- Add installed wheel and source-distribution smoke tests.
- Add POSIX owner-only permissions for the default history directory, database, and exported baselines.

### Changed

- Preserve the scenario `version: 1` format and legacy fingerprints when the default run timeout is used.
- Preserve scenario order in reports while allowing independent events to execute concurrently.
- Require explicit opt-in for remote HTTPS and a second opt-in for non-loopback plaintext HTTP.
- Keep redirects disabled and stream response bodies with a 2 MiB decoded-body limit.
- Pin repository and generated GitHub Actions dependencies to reviewed full commit SHAs, including the scaffolded HookStorm install reference.
- Require release tags to point to commits reachable from `main`; publish only from a published GitHub Release through Trusted Publishing.

### Fixed

- Reject duplicate YAML mapping keys, unsafe header values, duplicate event IDs, malformed URL ports, and invalid scenario limits before sending traffic.
- Cancel in-flight work when the run-wide deadline expires.
- Reject duplicate delivery identities and inconsistent metrics in history and imported baselines.
- Preserve sanitized history and baseline data without persisting request payloads, headers, response bodies, or credentials.
- Keep generated workflow writes inside the project directory and refuse overwrite by default.
- Preserve the default run-time fingerprint shape for existing baselines.

### Security

- Keep target validation local-only by default and require explicit flags for remote delivery.
- Reject credentials embedded in target URLs and keep query strings out of reports and history.
- Resolve signing and verification credentials from environment variables and omit their values from reports and history.
- Restrict stored history fields to an explicit metadata-and-outcomes whitelist.
