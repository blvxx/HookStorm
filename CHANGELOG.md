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
