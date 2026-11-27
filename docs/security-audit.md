# Security and robustness audit

**Audit date:** 2026-11-27  
**Reviewed revision:** `baf5a6bd363de3f0106aa185fa7cb32281292f98`  
**Scope:** focused source, configuration, transport, framework-adapter, fuzz-output, CI-template, packaging, and regression-test review on PR #44.

## Executive assessment

The audit found several concrete correctness and security weaknesses that were not covered by the previous test suite. The fixes below are present on the reviewed PR branch and passed the validation matrix listed at the end of this document.

This is a focused repository audit, not an independent penetration test or a security certification. It did not test against production or customer endpoints, inspect any private deployment environment, or prove safety for every HTTP implementation, provider, or operating system.

## Findings and remediation

| Severity | Finding | Status |
|---|---|---|
| High | IDN hostname normalization could cause the DNS pin key to differ from the hostname used by HTTPX/httpcore. The transport could then fall back to a fresh DNS lookup, reopening a DNS-rebinding window. | Fixed: normalize hostnames to IDNA ASCII using the same canonical form, resolve that form, and fail closed if the transport asks for an unpinned host. |
| High | The generated and example staging workflows allowed a same-repository pull request to enter a job with the `staging` environment and its secrets. Fork-only protection did not protect against same-repository PR code. | Fixed: PRs now run a separate scenario-validation-only job with no staging environment or secrets. Staging integration runs only on pushes to `main` or manual dispatches on `main`. |
| High | HTTPX automatic response decompression could expand a small compressed response before HookStorm checked its 2 MiB response-body cap. | Fixed defensively: HookStorm requests identity encoding, reads raw response bytes, and rejects compressed responses before JSON parsing. |
| Medium | Several request-fault attempt lists were not checked against the retry budget. A malformed scenario could configure a fault on an attempt that could never execute. | Fixed: all supported fault-plan lists are checked against `retry.max_attempts`; regression coverage enumerates every fault type. |
| Medium | Some malformed verification URLs could pass scenario validation and fail later during execution, including malformed host/port forms. | Fixed: verification URL validation now uses the same URL safety parser as delivery targets and converts errors into scenario validation failures. |
| Medium | `hookstorm init` accepted event type/ID strings that its own scenario validator would later reject. | Fixed: the generator validates nonblank printable ASCII values and has negative-case tests. |
| Medium | The framework recorder redacted standard authorization/signature headers but did not cover common API-key/token and custom secret-like header names. It also had no aggregate body-memory or header-size budget. | Fixed: expanded redaction rules, 64-header / 16 KiB header budgets, an 8 MiB aggregate raw-plus-decoded body budget, and bounded event count. |
| Medium | Fuzz replay files were created with the platform's default file mode, potentially exposing scenario payloads or custom headers to other local users. | Fixed on POSIX: replay files are created exclusively with mode `0600`; existing files are never overwritten. |
| Medium | YAML loading had no source-file, alias-count, or aggregate-payload limits. | Fixed: 16 MiB source-file cap, 16-alias cap, 1 MiB per-event payload cap, and 16 MiB aggregate payload cap. |

## Important compatibility and maintenance risks that remain

### HTTPX / httpcore private transport internals

DNS pinning currently wraps a network backend obtained from HTTPX's transport pool through a private attribute. The audit makes the behavior fail closed, but does not remove the dependency on an internal implementation detail. A future HTTPX/httpcore refactor may break the adapter or require a code change.

**Plan:**
1. Design a transport seam using supported HTTPX APIs where possible, or isolate the private adapter behind a small compatibility module.
2. Add explicit CI coverage for the minimum supported HTTPX release and the current newest compatible release.
3. Keep regression tests for DNS rebinding, IDNA, IPv4/IPv6, redirects, TLS hostname verification, and unpinned-host rejection.
4. Do not widen the HTTPX version range or claim compatibility with an untested internal layout.

### Compressed responses

The safe default now rejects all compressed response bodies, even small ones. This prevents uncontrolled automatic decompression but means a test endpoint that insists on gzip/deflate responses cannot currently be checked by response JSON assertions.

**Plan:** only add support if needed, using a bounded streaming decompressor that caps output before allocating it, validates stream completion/trailing data, and has expansion-ratio and regression tests. Do not re-enable `aiter_bytes()` with an after-the-fact size check.

### Protocol and fault-model boundaries

The current wire-level fault matrix targets HTTP/1.1. Slow streaming uploads, malformed HTTP/2 frames, arbitrary packet loss, kernel/network-device failures, and secret rotation during a run are not implemented. The deterministic client-side response-loss fault is not a substitute for a real network proxy. The demo's receiver-side commit-then-drop route remains a deterministic fixture, not a full distributed-systems simulator.

**Plan:** prioritize slow streamed uploads and secret-rotation semantics only when user applications need them. HTTP/2 support should be a separately scoped design and test effort, not an extension of the HTTP/1.1 framing adapter.

### Framework adapters

FastAPI and Flask integrations remain optional, in-memory test receivers. They do not verify provider signatures, persist events, or replace application webhook handlers. Their recorder limits reduce accidental memory growth but are not a substitute for production ingress limits, authentication, rate limiting, or durable processing.

## Validation evidence

The exact reviewed PR revision `baf5a6bd363de3f0106aa185fa7cb32281292f98` passed:

- CI matrix: 7/7 jobs — Ubuntu Python 3.11, 3.12, 3.13, 3.14; Windows Python 3.12; macOS Python 3.12; Docker non-root/read-only smoke.
- Tests: 533 passed on Ubuntu and macOS; Windows: 530 passed, 3 platform-specific skips.
- Coverage: 86%.
- Ruff and mypy: passed in every test environment.
- Wheel/source distribution build and clean-install smoke: passed.
- Zensical documentation build, local webhook scenario workflow, and Composite GitHub Action E2E: passed.

CI run: [46009605485](https://github.com/blvxx/HookStorm/actions/runs/46009605485).

## Release blockers and plan

The audit fixes are on draft PR #44; they are not yet in `main`. Do not tag or publish from this branch.

1. Review and merge PR #44 only after the maintainer is satisfied with its diff and workflow results.
2. After merge, update `HOOKSTORM_INSTALL_REF` in `src/hookstorm/scaffold.py` to the exact reviewed main commit. Regenerate a sample project and verify that its workflow installs that exact commit.
3. Re-run the full matrix, webhook integration, Composite Action E2E, documentation build, Docker smoke, and release preflight on the exact main commit.
4. Verify PyPI project-name ownership and configure the Trusted Publisher and protected publishing environment.
5. Configure GitHub Pages and verify the first deployment before advertising a documentation URL.
6. Refresh and scan the Docker base image before any container registry publication.
7. Only then consider the `v0.1.0a1` pre-release. Do not skip the gates because the current PR CI is green.

Engineering readiness does not establish independent adoption or willingness to pay. Product validation remains a separate experiment described in [User validation](user-validation.md).
