# Installation

HookStorm requires Python 3.11 or newer. Choose a method based on how you want to run the CLI.

## Install from a source checkout

This works before the first PyPI publication and is the most direct route for contributors:

~~~bash
git clone https://github.com/blvxx/HookStorm.git
cd HookStorm
python -m pip install .
hookstorm --version
~~~

For development, install the full test toolchain:

~~~bash
python -m pip install -e '.[dev,fastapi,flask,docs]'
~~~

The FastAPI, Flask, and documentation dependencies are optional and are not installed by a normal HookStorm installation.

## Install with uv

After the first alpha distribution is published to PyPI, install the persistent CLI tool:

~~~bash
uv tool install hookstorm
hookstorm --version
~~~

To run a one-off command without installing the tool into your environment:

~~~bash
uvx --from hookstorm hookstorm --help
uvx --from hookstorm hookstorm schema
~~~

Install an optional framework adapter when you need its test endpoint:

~~~bash
uv tool install 'hookstorm[fastapi]'
# or
uv tool install 'hookstorm[flask]'
~~~

Upgrade or remove the tool:

~~~bash
uv tool upgrade hookstorm
uv tool uninstall hookstorm
~~~

For a source checkout, use `uv tool install .` from the repository root.

## Install with pipx

After the first alpha distribution is published to PyPI:

~~~bash
pipx install hookstorm
hookstorm --version
~~~

Optional adapters can be installed as extras:

~~~bash
pipx install 'hookstorm[fastapi]'
# or
pipx install 'hookstorm[flask]'
~~~

Upgrade or remove HookStorm:

~~~bash
pipx upgrade hookstorm
pipx uninstall hookstorm
~~~

For a source checkout, run `pipx install .` from the repository root.

## Run with Docker

Build the image from the repository root:

~~~bash
docker build -t hookstorm:local .
docker run --rm hookstorm:local --version
docker run --rm hookstorm:local --help
~~~

The image uses a non-root user (UID 10001), contains only the core HookStorm package, and starts with the `hookstorm` CLI as its entrypoint. Optional FastAPI/Flask dependencies are not included in the runtime image.

Validate a scenario from a read-only bind mount:

~~~bash
docker run --rm --read-only --tmpfs /tmp \
  -v "$PWD/examples:/examples:ro" \
  hookstorm:local validate /examples/payment-retry.yaml
~~~

To write a report, mount a writable output directory and pass an explicit output path:

~~~bash
mkdir -p reports
docker run --rm --read-only --tmpfs /tmp \
  -v "$PWD/examples:/examples:ro" \
  -v "$PWD/reports:/reports" \
  hookstorm:local run /examples/payment-retry.yaml \
  --target https://your-test-endpoint.example/webhook \
  --allow-remote --output /reports/hookstorm-report.html
~~~

Remote HTTPS still requires `--allow-remote`; non-loopback plaintext HTTP additionally requires `--allow-insecure-http`. A container's loopback address is the container itself. Do not weaken target checks just to reach a host service: use a dedicated test network and only explicit opt-ins for endpoints you own or are authorized to test.

The container's working directory is writable by its non-root user. Use read-only mounts for scenarios and a separate writable mount for reports or exported baselines.

## PyPI publication status

The commands that install `hookstorm` by package name assume that the first alpha distribution is available on PyPI. Until the package page and uploaded artifacts are verified after publication, use the source-checkout instructions above or build the Docker image locally. See the [PyPI publishing guide](pypi-publishing.md).
