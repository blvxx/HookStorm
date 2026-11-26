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
