# Alpha release readiness review

**Review date:** 2026-11-27  
**Package version:** `0.1.0a1`  
**Scope:** current source state on PR #44 and its release / distribution path.

## Decision

**Decision: NO-GO — do not publish yet.**

The project is an **alpha candidate** from an engineering perspective, but this review does not authorize a GitHub Release, PyPI upload, or public container push. The changes are on a draft pull request, not on `main`. The release workflow deliberately requires a published release tag to point to a commit reachable from `main`.

After the PR is reviewed and merged, the final release commit must pass the release gates again. Re-run this review against that exact commit before tagging.

## Evidence already present in the repository

| Area | Evidence | Assessment |
|---|---|---|
| Package metadata | `pyproject.toml` and `hookstorm.__version__` identify `0.1.0a1` | Consistent at review time |
| Core install | Runtime dependencies are limited to HTTPX and PyYAML | Good separation |
| Optional integrations | FastAPI, Flask, pytest, and docs are separate extras | Core install remains lightweight |
| Automated tests | Six-platform CI matrix; optional framework adapters are included in CI | Must pass on the final PR head |
| Static checks | Ruff and mypy run in CI | Required release gates |
| Distributions | Wheel and sdist are built and smoke-installed in CI | Required release gates |
| Release workflow | Tag/version/prerelease validation, main-ancestry check, Twine validation, and PyPI Trusted Publishing | Fail-closed design |
| Docker | Multi-stage runtime image, non-root UID 10001, read-only container smoke in CI | Local image only; no registry publication |
| Documentation | Zensical configuration and a separate build workflow | Site source is prepared; not deployed |
| Secrets | Reports/history use explicit safe fields; integrations redact sensitive headers by default | Review payloads before sharing |

## Mandatory blockers before publication

1. **Merge and review.** PR #44 is still a draft. Do not tag this feature branch. Merge only after review, then use the resulting commit reachable from `main`.
2. **Refresh the scaffold install pin.** `src/hookstorm/scaffold.py` currently pins generated workflows to an earlier HookStorm commit. After merge, update `HOOKSTORM_INSTALL_REF` to the final reviewed commit on `main`, regenerate a sample project, and verify that its workflow installs that exact revision.
3. **Re-run exact-commit validation.** Run the six-environment CI matrix, webhook scenario integration workflow, Composite Action smoke, documentation build, and Docker smoke on the final release commit. Then run the Release workflow manually as a build-only preflight.
4. **Verify PyPI ownership and availability.** Confirm the `hookstorm` name is still available or belongs to the intended project owner. Configure the pending Trusted Publisher and protected `pypi` GitHub Environment. The existence and ownership of a PyPI project were not established by this repository review.
5. **Verify the public documentation destination.** A Pages workflow now builds Zensical and deploys only after a push to `main`, using a dedicated `github-pages` environment. Repository Pages configuration and a successful first deployment have not been verified; do not advertise the site URL until those checks are complete.
6. **Keep publication actions separate.** Create an annotated `v0.1.0a1` tag and a GitHub pre-release only from the reviewed main commit. PyPI publishing occurs only after the Release is published and Trusted Publishing is configured. Do not publish from PR CI.

## Technical risks and known limitations

- **HTTPX internals:** DNS pinning wraps a private network-backend attribute exposed by the HTTPX transport pool. CI currently protects the behavior, but an HTTPX internal refactor may require changes. Consider replacing this adapter with a supported transport seam in a future release.
- **Transport scope:** request-level wire faults currently target HTTP/1.1. Slow streamed bodies, arbitrary packet loss, malformed HTTP/2 frames, and kernel/network-device failures are not modeled.
- **Framework helpers:** FastAPI and Flask adapters are opt-in test receivers. They are in-memory, have bounded request/event storage, redact sensitive headers by default, and do not verify provider signatures or replace application handlers. They must not be enabled as production webhook endpoints.
- **Container lifecycle:** the Docker image is built locally in CI and is not published to a registry. Its base image uses the maintained `python:3.12-slim` tag; refresh the base and scan it before any public image distribution.
- **Documentation lifecycle:** Zensical builds the configured `mkdocs.yml` project. A main-only deployment workflow is prepared with pinned official Pages actions and least-privilege build/deploy jobs. GitHub Pages must still be enabled/configured, the `github-pages` environment must be allowed to deploy, and the first main-branch deployment must succeed.
- **PyPI:** package-name ownership, Trusted Publisher setup, and actual uploaded artifacts remain unverified until the owner performs the account-side setup and publishes the first release.

## Release acceptance checklist

- [ ] PR #44 reviewed, approved, and merged to `main`.
- [ ] `HOOKSTORM_INSTALL_REF` updated to the exact reviewed main commit and generated workflow retested.
- [ ] Version in `pyproject.toml`, `src/hookstorm/__init__.py`, tag, and changelog all match.
- [ ] Six-platform CI, webhook integration, Composite Action, docs build, and Docker smoke are green on the exact release commit.
- [ ] Wheel and sdist pass clean-install smoke tests; Twine metadata validation passes.
- [ ] No secrets, private endpoint URLs, local databases, or generated reports are included in artifacts.
- [ ] PyPI publisher ownership, Trusted Publisher, and protected environment are verified.
- [ ] GitHub Pages is configured for GitHub Actions, the `github-pages` environment is allowed to deploy, and the first Zensical deployment is verified before the docs URL is advertised.
- [ ] Release notes describe the alpha status, supported Python versions, security defaults, and known limitations.
- [ ] GitHub Release is marked as a pre-release and points to a tag reachable from `main`.

**Current recommendation:** keep the project in draft / pre-release preparation. The remaining blockers are explicit and verifiable; do not bypass them to accelerate the first upload.
