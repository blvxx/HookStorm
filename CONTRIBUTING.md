# Contributing

Thanks for helping improve HookStorm.

## Before opening an issue

- Search existing issues to avoid duplicates.
- Include Python version, HookStorm version, operating system, command, and a minimal scenario. Remove secrets and customer data.
- For unexpected delivery behavior, include a small local receiver or repeatable test case.

## Development setup

~~~bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
~~~

Before submitting a pull request:

~~~bash
ruff check .
ruff format --check .
pytest
~~~

## Pull requests

- Keep changes focused and explain the behavior being changed.
- Add tests for bug fixes and new behavior.
- Update the scenario format docs and README when user-facing configuration changes.
- Do not add telemetry, cloud dependencies, or implicit remote network behavior.
- Never commit API keys, HMAC secrets, real webhook payloads, or production URLs.

Contributions are provided under the project's MIT license.
