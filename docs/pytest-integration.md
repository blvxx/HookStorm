# Pytest integration API

HookStorm includes an opt-in pytest plugin for application repositories that want to run webhook scenarios from ordinary test suites. The plugin is separate from the core CLI and does not add pytest to a normal HookStorm installation.

## Install and enable

Install the optional extra:

~~~bash
python -m pip install "hookstorm[pytest]"
~~~

Enable the plugin from your project's root conftest.py:

~~~python
pytest_plugins = ["hookstorm.pytest_plugin"]
~~~

The plugin is not auto-loaded. Projects that do not opt in do not import pytest through HookStorm and do not receive additional pytest command-line behavior.

## Configure the target

Set a dedicated test or staging endpoint:

~~~bash
# Linux / macOS
export HOOKSTORM_TARGET_URL="http://127.0.0.1:8000/webhook"
export HOOKSTORM_SECRET="local-test-secret"

# PowerShell
$env:HOOKSTORM_TARGET_URL = "http://127.0.0.1:8000/webhook"
$env:HOOKSTORM_SECRET = "local-test-secret"
~~~

The plugin accepts HOOKSTORM_TARGET_URL and retains HOOKSTORM_TARGET for compatibility with files created by hookstorm export-pytest. The explicit URL variable takes precedence.

Remote testing is off by default. Set HOOKSTORM_ALLOW_REMOTE=1 only for an endpoint you own or are authorized to test. Non-loopback plaintext HTTP additionally requires HOOKSTORM_ALLOW_INSECURE_HTTP=1; prefer HTTPS. These flags are passed through to the same target safety checks used by the CLI. Tests are skipped when no target is configured, so importing the plugin does not send traffic.

By default the signing secret is read from HOOKSTORM_SECRET. Set HOOKSTORM_SECRET_ENV to use another environment variable name. Secret values are never embedded in the scenario or test module.

## Fixtures and example

~~~python
from pathlib import Path

import pytest

@pytest.mark.integration
def test_invoice_webhook(hookstorm, hookstorm_scenario):
    scenario = hookstorm_scenario(Path("scenarios/invoice-paid.yaml"))
    result = hookstorm.run_sync(scenario)

    assert result["summary"]["success"], result
~~~

Available fixtures:

| Fixture | Purpose |
|---|---|
| hookstorm_target | Configured target URL; skips the test if neither target variable is set |
| hookstorm | HookStormRunner configured with target safety flags and environment-backed secret |
| hookstorm_scenario | Loader for a YAML path or an in-memory scenario mapping |

## Async tests

The runner also exposes an async method:

~~~python
@pytest.mark.asyncio
async def test_invoice_webhook_async(hookstorm, hookstorm_scenario):
    scenario = hookstorm_scenario("scenarios/invoice-paid.yaml")
    result = await hookstorm.run(scenario)

    assert result["summary"]["success"], result
~~~

Install and configure an async pytest plugin such as pytest-asyncio in your application repository to execute async test functions. HookStorm does not require or install an async pytest plugin.

For synchronous tests, run_sync() owns the event loop and is convenient with normal pytest functions. It fails with an actionable message when called from an active event loop; use await run() in async tests instead.

## Safety and CI guidance

- Use synthetic event payloads and a dedicated staging/test receiver. Do not point integration tests at production payment, identity, or customer-facing systems.
- Do not place secrets in YAML, pytest parameters, fixture values, or source control. Use the CI provider's secret store.
- Keep remote access explicitly opted in and prefer HTTPS. The fixture does not weaken HookStorm's target validation.
- Keep integration tests separately marked so unit-only runs can exclude them with pytest -m "not integration".
- The plugin does not start or manage FastAPI, Flask, databases, queues, or other application dependencies. Start the receiver using your normal test fixtures or application test client.

## Relationship to hookstorm export-pytest

hookstorm export-pytest remains available for a standalone generated test module. The plugin API is intended for teams that want to reuse scenarios, fixtures, and target configuration across multiple test functions without embedding large scenario literals in generated files.
