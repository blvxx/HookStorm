# Composite GitHub Action

HookStorm provides a reusable composite action at .github/actions/run. It installs the version of HookStorm pinned by the action reference, validates a scenario, optionally performs a non-invasive DNS/TCP/TLS preflight, runs the scenario, and exposes the report path as an action output.

## Quick start

Copy the workflow template from examples/ci/hookstorm-action.yml into your application repository and review the action reference. The template currently pins the HookStorm action to commit 7a1006aa943afdb4afb8eb67b10393a51f2319cb; use a full reviewed commit SHA when upgrading it.

Create a GitHub Environment named staging. Add:

| Name | Type | Purpose |
|---|---|---|
| HOOKSTORM_TARGET_URL | Variable | Dedicated test or staging webhook URL |
| HOOKSTORM_SECRET | Secret | Signing secret, only when the scenario requires one |

Configure required reviewers and branch restrictions on the environment. The sample skips fork-originated pull requests because those runs do not have staging secrets. Never use a production endpoint or production signing secret.

## Inputs

| Input | Default | Purpose |
|---|---|---|
| scenario | required | Scenario YAML path in the caller's checked-out workspace |
| target-url | empty | Target URL; falls back to HOOKSTORM_TARGET_URL |
| python-version | 3.12 | Python runtime |
| allow-remote | false | Explicit permission for remote HTTPS |
| allow-insecure-http | false | Explicit permission for non-loopback HTTP; requires allow-remote=true |
| connect-preflight | false | Perform DNS, TCP and TLS checks without sending an HTTP request |
| secret-env | HOOKSTORM_SECRET | Environment variable name that holds the signing secret |
| format | html | html, json, or junit |
| output-path | hookstorm-report.html | Generated report path |
| history-db | empty | Optional SQLite path; history is disabled by default |

The action output report-path contains the report path. Pass the signing secret through the calling step's env block, not through a scenario file or action input.

## Example

~~~yaml
- name: Run HookStorm
  id: hookstorm
  uses: blvxx/HookStorm/.github/actions/run@7a1006aa943afdb4afb8eb67b10393a51f2319cb
  with:
    scenario: scenarios/webhook.yaml
    target-url: ${{ vars.HOOKSTORM_TARGET_URL }}
    allow-remote: "true"
    connect-preflight: "true"
    format: junit
    output-path: test-results/hookstorm.xml
  env:
    HOOKSTORM_SECRET: ${{ secrets.HOOKSTORM_SECRET }}
~~~

Upload the output with your usual artifact action, using the report-path output. The included workflow template shows a complete checkout, run, and artifact-upload sequence with full-SHA-pinned actions.

## Safety behavior

- Remote access is denied unless allow-remote is explicitly true.
- Non-loopback plaintext HTTP additionally requires allow-insecure-http=true; the action rejects an inconsistent input combination before sending traffic.
- The optional connectivity preflight uses HookStorm doctor and sends no HTTP request or webhook event.
- Redirects remain disabled and URL credentials are rejected by HookStorm.
- History persistence is disabled unless history-db is provided.
- The action does not deploy your application, start your receiver, or provision secrets. Prepare the dedicated staging endpoint in your normal deployment workflow.

The action uses actions/setup-python pinned to a reviewed full commit SHA. Pin the HookStorm action itself to a full commit SHA in consumer workflows; do not reference a mutable branch or tag for supply-chain-sensitive CI.
