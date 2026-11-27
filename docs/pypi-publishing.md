# Publishing HookStorm to PyPI

The package metadata currently identifies the project as **0.1.0a1**, the intended first alpha release. As of **November 27, 2026**, the repository has no published GitHub Release or release tag. The PyPI project page could not be independently verified during this review, so the availability of `hookstorm==0.1.0a1` remains unconfirmed. Do not use or advertise a PyPI install command until the project page and uploaded artifacts have been verified after publication.

The repository includes a release workflow. It does not publish on pushes or pull requests. A manual workflow run preflights lint, tests, and package building; actual PyPI publication starts only after a GitHub Release is published.

## One-time account setup

1. Sign in to PyPI with the account that will own the `hookstorm` project.
2. Open **Account settings → Publishing** and add a **pending publisher**:
   - PyPI project name: `hookstorm`
   - Owner: `blvxx`
   - Repository: `HookStorm`
   - Workflow filename: `release.yml`
   - Environment: `pypi`
3. In the GitHub repository, open **Settings → Environments** and create an environment named `pypi`. Add deployment protection and manual approval where appropriate.
4. Keep the pending publisher configuration aligned with the repository owner, repository, workflow filename, and environment. The first successful publish creates the PyPI project and converts the pending publisher into a normal publisher.

A pending publisher does **not** reserve the name before the first successful upload. Check the package name again immediately before the first release.

## Release procedure

1. Confirm CI and the webhook integration workflow pass on the exact commit intended for release.
2. Confirm `pyproject.toml`, `src/hookstorm/__init__.py`, and the changelog identify the same release version.
3. Create an annotated tag `v0.1.0a1` on the reviewed `main` commit.
4. Create a GitHub Release from that tag, mark it as a **pre-release**, and include the matching changelog notes.
5. Publish the GitHub Release. This triggers `.github/workflows/release.yml`.

Before the first release, use **Actions → Release → Run workflow** on the reviewed branch to exercise the build and metadata checks. A manual run is build-only; the PyPI publishing job is conditional on a published GitHub Release event.

The workflow checks that the release tag exactly matches the version in `pyproject.toml` and that the GitHub pre-release flag agrees with the PEP 440 version. It runs lint and tests, builds both wheel and source distribution, validates them with Twine, and smoke-tests clean installations. A separate least-privilege job attaches the validated distributions to the GitHub Release. The PyPI job downloads those same build artifacts and publishes them through Trusted Publishing.

## Authentication and workflow security

- PyPI authentication uses Trusted Publishing (GitHub Actions OIDC); no PyPI API token or password is stored in GitHub secrets.
- The PyPI publishing job alone receives `id-token: write`.
- The GitHub Release asset job receives only `contents: write` to attach the validated distributions.
- Build and test steps run in a separate job without OIDC publishing permission.
- The release workflow actions are pinned to reviewed commit SHAs.
- The `pypi` environment provides a place to apply GitHub deployment protection and manual approval.

Do not add a long-lived PyPI token to repository secrets. If publisher configuration is not ready, the release workflow fails closed instead of falling back to token authentication.

## Post-publication verification

After the workflow succeeds, confirm that the public project page lists the intended release and both distribution files. Then install the published artifact in a clean environment:

~~~bash
python -m pip install --pre --upgrade hookstorm
hookstorm --version
python -m pip show hookstorm
~~~

Also verify the release notes, uploaded wheel and source archive, and provenance / attestations on PyPI.

## Official references

- [Create a new PyPI project with a Trusted Publisher](https://docs.pypi.org/trusted-publishers/creating-a-project-through-oidc/)
- [Configure a Trusted Publisher for an existing project](https://docs.pypi.org/trusted-publishers/adding-a-publisher/)
- [Publish with a Trusted Publisher](https://docs.pypi.org/trusted-publishers/using-a-publisher/)
- [PyPA GitHub Action for PyPI publishing](https://github.com/pypa/gh-action-pypi-publish)
