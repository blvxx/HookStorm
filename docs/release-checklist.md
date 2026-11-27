# Release checklist

HookStorm is currently in the pre-release stage. Do not publish a stable release until every required gate below is complete.

## Before tagging

- [ ] Merge all reviewed reliability, security, and packaging changes into `main`.
- [ ] Confirm the full CI matrix and the webhook scenario workflow are green on the exact release commit.
- [ ] Confirm every repository and generated GitHub Actions workflow pins third-party actions to reviewed full commit SHAs, and the scaffolded HookStorm install pin matches the reviewed revision.
- [ ] Confirm `pyproject.toml` and `hookstorm.__version__` agree with the intended release version.
- [ ] Move the corresponding changelog entries from **Unreleased** into a dated/versioned section.
- [ ] Build both wheel and source distribution with `python -m build`.
- [ ] Install both wheel and source distribution in clean virtual environments; run `hookstorm --version`, `hookstorm init --no-workflow`, and `hookstorm validate` on the generated scenario without relying on source-checkout files.
- [ ] Review README installation instructions, supported Python versions, security defaults, and known limitations.
- [ ] Confirm no credentials, private endpoint URLs, local database files, or generated reports are included in the release artifacts.

## GitHub release

1. Create an annotated tag from the reviewed `main` commit, using the `vX.Y.Z` format.
2. Create a GitHub Release from that tag.
3. Include the relevant changelog notes and mark the release as a pre-release until the project owner explicitly considers the compatibility and support commitments stable.
4. Publish the GitHub Release. The release workflow attaches the validated wheel and source distribution automatically after the build checks pass.

## Optional PyPI publication

PyPI publishing is not enabled by this checklist alone. Before enabling it:

- Configure a PyPI Trusted Publisher for this repository and the dedicated publishing workflow.
- Use GitHub Actions OIDC; do not store a long-lived PyPI API token in repository secrets.
- Publish only from a GitHub Release event after verifying the tag points to the reviewed commit.
- Verify the uploaded artifacts by installing the released version in a fresh environment.

## Post-release verification

- [ ] Install the published artifact in a clean environment on Python 3.11+.
- [ ] Run `hookstorm --version`, `hookstorm validate`, and a local receiver integration scenario.
- [ ] Verify the release page, installation commands, and changelog links.
- [ ] Open follow-up issues for deferred features rather than silently changing the release scope.
