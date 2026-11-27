# Security policy

## Scope

HookStorm sends HTTP requests to a user-configured webhook endpoint. The default policy permits loopback targets only. Remote HTTPS requires the explicit `--allow-remote` flag; non-loopback HTTP additionally requires `--allow-insecure-http` so plaintext transport cannot be enabled accidentally. Authenticated remote verification probes always require HTTPS. The project does not operate a hosted relay or collect telemetry.

## Reporting a vulnerability

Please do not disclose exploitable vulnerabilities in a public issue. Use GitHub's private vulnerability reporting feature for this repository, when available. Include affected versions, impact, and a minimal reproduction. Do not include real credentials or customer payloads.

## Safe use

- Use isolated development or staging endpoints.
- Do not run chaos scenarios against production payment, identity, or critical infrastructure endpoints.
- Treat report files as potentially sensitive because they contain endpoint URLs and event identifiers.
- Configure verification credentials only through environment variables; never put tokens or API keys in scenario files. HookStorm resolves them before sending webhook traffic and does not persist credential values. Authenticated remote verification endpoints must use HTTPS; plaintext HTTP is restricted to localhost / loopback.
- The included demo receiver is for local use only.
