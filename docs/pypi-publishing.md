# Publishing HookStorm to PyPI

HookStorm's first planned public package is **0.1.0a1**, a pre-release. The repository contains a release workflow, but it does not publish on pushes or pull requests. You can run the workflow manually to preflight lint, tests, and package building; that mode skips PyPI publication. Actual publication starts only after a GitHub Release is published.

## One-time account setup

1. Sign in to PyPI with the account that will own the `hookstorm` project.
2. Open **Account settings → Publishing** and add a **pending publisher**:
   - PyPI project name: `hookstorm`
   - Owner: `blvxx`
   - Repository: `HookStorm`
   - Workflow filename: `release.yml`
   - Environment: `pypi`
3. In the GitHub repository, open **Settings → Environments** and create an environment named `pypi`. Add required reviewers / deployment protection rules where appropriate.
4. Keep the pending publisher configuration aligned with the repository owner, repository, workflow filename, and environment. The first successful publish creates the PyPI project and converts the pending publisher into a normal publisher.

A pending publisher does **not** reserve the name before the first successful upload. The package name should therefore be checked again immediately before the first release.

## Release procedure

1. Merge the reviewed reliability and baseline-integrity pull requests into `main`.
2. Confirm CI and the webhook integration workflow pass on the exact commit intended for release.
3. Confirm `pyproject.toml`, `src/hookstorm/__init__.py`, and the changelog all identify the same release version.
4. Create an annotated tag `v0.1.0a1` on that reviewed `main` commit.
5. Create a GitHub Release from that tag, mark it as a **pre-release**, and include the matching `0.1.0a1` changelog notes.
6. Publish the GitHub Release. This triggers `.github/workflows/release.yml`.

Before the first release, use **Actions → Release → Run workflow** on the reviewed branch to exercise the build and metadata checks. A manual run is build-only; the PyPI publishing job is conditional on a published GitHub Release event.

The workflow checks that the release tag exactly matches the version in `pyproject.toml` and that the GitHub pre-release flag agrees with the PEP 440 version. It then runs lint and tests, builds both wheel and source distribution, validates them with Twine, and uploads the already-built artifacts to PyPI in a separate job.

## Authentication and workflow security

- PyPI authentication uses Trusted Publishing (GitHub Actions OIDC); no PyPI API token or password is stored in GitHub secrets.
- The publishing job alone receives `id-token: write`.
- Build and test steps run in a separate job without OIDC publishing permission.
- The release workflow actions are pinned to reviewed commit SHAs.
- The `pypi` environment provides a place to apply GitHub deployment protection and manual approval.

Do not add a long-lived PyPI token to repository secrets. If the publisher configuration is not ready, the release workflow will fail closed at the PyPI publishing step rather than falling back to token authentication.

## Post-publication verification

After the workflow succeeds:

~~~bash
python -m pip install --pre --upgrade hookstorm
hookstorm --version
python -m pip show hookstorm
~~~

Also verify the project page, uploaded wheel and source archive, release notes, and provenance / attestations on PyPI.

## Official references

- [Create a new PyPI project with a Trusted Publisher](https://docs.pypi.org/trusted-publishers/creating-a-project-through-oidc/)
- [Configure a Trusted Publisher for an existing project](https://docs.pypi.org/trusted-publishers/adding-a-publisher/)
- [Publish with a Trusted Publisher](https://docs.pypi.org/trusted-publishers/using-a-publisher/)
- [PyPA GitHub Action for PyPI publishing](https://github.com/pypa/gh-action-pypi-publish)
