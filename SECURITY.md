# Security policy

## Supported versions

| Version | Supported |
|---|---|
| Latest published version | Yes |
| Older versions and unreleased commits | No; update to the latest published version before reporting a vulnerability |

HookStorm is an alpha project. Security fixes are provided for the latest published version only.

## Scope and safe use

HookStorm sends HTTP requests to a user-configured webhook endpoint. By default, only loopback targets are permitted. Remote HTTPS requires the explicit `--allow-remote` flag; non-loopback HTTP additionally requires `--allow-insecure-http`. Authenticated remote verification probes always require HTTPS. Redirects are disabled, and responses are streamed with a 2 MiB decoded-body limit.

Use isolated development or staging endpoints that you own or are authorized to test. Do not run chaos scenarios against production payment, identity, or critical infrastructure endpoints. The included demo receiver is for local use only.

Signing secrets and verification credentials are read from environment variables, not scenario literals. HookStorm does not include credential values in reports or history. Reports and history retain the target URL scheme, hostname, explicit port (when present), and path; query strings and fragments are removed. Event identifiers and URL paths may still contain sensitive information, so treat generated artifacts as potentially sensitive.

## Reporting a vulnerability

Please do not disclose exploitable vulnerabilities in a public issue or pull request.

Send vulnerability reports privately to [enotzz@bk.ru](mailto:enotzz@bk.ru) with the subject **[HookStorm Security]**. Include the affected version or commit, impact, reproduction steps, and any relevant sanitized logs. Do not include real credentials, customer payloads, or production data. The maintainer will acknowledge the report and coordinate a fix and disclosure timeline.

## Data handling

- Request payloads, custom request headers, response bodies, and secret values are not persisted in the built-in reports or run history.
- The target URL scheme, hostname, explicit port (when present), and path are retained; query strings and fragments are stripped.
- Run history is stored locally. On POSIX, the default history directory is owner-only (`0700`) and the database and exported baseline files are owner-only (`0600`).
