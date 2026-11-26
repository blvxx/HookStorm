"""Project scaffolding for a first HookStorm integration."""

from __future__ import annotations

import json
import re
from pathlib import Path, PureWindowsPath

HOOKSTORM_INSTALL_REF = "d7d214586885e98604e7a2abed36e51b9dd2c8ec"
_SIGNATURE_PROVIDERS = {"generic", "stripe", "github", "shopify"}
_SAFE_PATH = re.compile(r"^[A-Za-z0-9._/-]+$")


def _relative_project_path(value: str, label: str) -> Path:
    if not value or not _SAFE_PATH.fullmatch(value):
        raise ValueError(
            f"{label} must be a relative path containing only letters, digits, '.', '_', '-', and '/'"
        )
    path = Path(value)
    windows_path = PureWindowsPath(value)
    if path.is_absolute() or windows_path.is_absolute() or ".." in path.parts:
        raise ValueError(f"{label} must stay inside the current project directory")
    if "." in path.parts:
        raise ValueError(f"{label} must not contain a standalone '.' path segment")
    return path


def render_scenario(
    *,
    event_type: str = "test.ping",
    event_id: str = "evt_hookstorm_init_001",
    signature_provider: str = "generic",
) -> str:
    if signature_provider not in _SIGNATURE_PROVIDERS:
        choices = ", ".join(sorted(_SIGNATURE_PROVIDERS))
        raise ValueError(f"Unsupported signature provider '{signature_provider}'. Choose: {choices}")
    if not event_type or len(event_type) > 256 or any(ch in event_type for ch in "\\r\\n"):
        raise ValueError("event type must contain 1–256 characters and no line breaks")
    if not event_id or len(event_id) > 256 or any(ch in event_id for ch in "\\r\\n"):
        raise ValueError("event ID must contain 1–256 characters and no line breaks")

    return f"""version: 1
name: HookStorm webhook contract
description: Replace the sample event and assertions with your staging webhook contract.
delivery:
  signature: valid
  signature_provider: {signature_provider}
  timeout_ms: 5000
retry:
  max_attempts: 1
events:
  - id: {json.dumps(event_type, ensure_ascii=False)}
    event_id: {json.dumps(event_id, ensure_ascii=False)}
    payload:
      test: true
      source: hookstorm
    expect_status: [200, 201, 202]
    expect_json:
      accepted: true
"""


def render_workflow(*, scenario_path: str, workflow_path: str) -> str:
    scenario = json.dumps(scenario_path, ensure_ascii=False)
    workflow = json.dumps(workflow_path, ensure_ascii=False)
    install = (
        "git+https://github.com/blvxx/HookStorm.git@"
        f"{HOOKSTORM_INSTALL_REF}"
    )
    return f"""name: Webhook regression

on:
  push:
    branches: [main]
    paths:
      - {scenario}
      - {workflow}
  pull_request:
    branches: [main]
    paths:
      - {scenario}
      - {workflow}
  workflow_dispatch:

permissions:
  contents: read

concurrency:
  group: hookstorm-webhook-regression-${{{{ github.ref }}}}
  cancel-in-progress: false

jobs:
  webhook-regression:
    # Fork PRs never receive staging secrets or call the staging endpoint.
    if: >-
      github.event_name != 'pull_request' ||
      github.event.pull_request.head.repo.full_name == github.repository
    runs-on: ubuntu-latest
    environment: staging
    env:
      HOOKSTORM_SCENARIO: ${{{{ vars.HOOKSTORM_SCENARIO || '{scenario_path}' }}}}
      HOOKSTORM_TARGET_URL: ${{{{ vars.HOOKSTORM_TARGET_URL }}}}
      HOOKSTORM_SECRET: ${{{{ secrets.HOOKSTORM_SECRET }}}}
      HOOKSTORM_LATENCY_THRESHOLD_PCT: ${{{{ vars.HOOKSTORM_LATENCY_THRESHOLD_PCT || '20' }}}}
      HOOKSTORM_HISTORY_DB: .hookstorm/history.sqlite3
      HOOKSTORM_BASELINE_FILE: .hookstorm-baselines/webhook.json
    steps:
      - name: Check out application
        uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1
        with:
          persist-credentials: false

      - name: Set up Python
        uses: actions/setup-python@6d4fd22b20465ccc0f7354476588e8df7c9ca0e0
        with:
          python-version: "3.12"

      - name: Install HookStorm
        run: >-
          python -m pip install
          "{install}"

      - name: Validate integration settings and scenario
        shell: bash
        run: |
          set -euo pipefail
          if [[ -z "${{HOOKSTORM_TARGET_URL:-}}" ]]; then
            echo "::error::Set the staging environment variable HOOKSTORM_TARGET_URL"
            exit 1
          fi
          if [[ -z "${{HOOKSTORM_SECRET:-}}" ]]; then
            echo "::error::Set the staging environment secret HOOKSTORM_SECRET"
            exit 1
          fi
          if [[ ! -f "$HOOKSTORM_SCENARIO" ]]; then
            echo "::error::Scenario file not found: $HOOKSTORM_SCENARIO"
            exit 1
          fi
          hookstorm validate "$HOOKSTORM_SCENARIO"

      - name: Restore main-branch baseline
        uses: actions/cache/restore@0057852bfaa89a56745cba8c7296529d2fc39830
        with:
          path: .hookstorm-baselines/
          key: hookstorm-baseline-main-${{{{ github.sha }}}}-${{{{ github.run_id }}}}
          restore-keys: |
            hookstorm-baseline-main-

      - name: Run webhook scenario and compare with baseline
        shell: bash
        run: |
          set -euo pipefail
          mkdir -p .hookstorm .hookstorm-baselines test-results

          hookstorm run "$HOOKSTORM_SCENARIO" \
            --target "$HOOKSTORM_TARGET_URL" \
            --allow-remote \
            --secret-env HOOKSTORM_SECRET \
            --history-db "$HOOKSTORM_HISTORY_DB" \
            --format junit \
            --output test-results/webhook-regression.xml

          is_trusted_main_run() {
            [ "$GITHUB_REF" = "refs/heads/main" ] && {
              [ "$GITHUB_EVENT_NAME" = "push" ] ||
              [ "$GITHUB_EVENT_NAME" = "workflow_dispatch" ]
            }
          }

          if [ -f "$HOOKSTORM_BASELINE_FILE" ]; then
            hookstorm history compare-file "$HOOKSTORM_BASELINE_FILE" \
              --db "$HOOKSTORM_HISTORY_DB" \
              --latency-threshold-pct "$HOOKSTORM_LATENCY_THRESHOLD_PCT"
            # PRs only read the main baseline. Trusted main runs may advance it.
            if is_trusted_main_run; then
              hookstorm history export latest \
                --output "$HOOKSTORM_BASELINE_FILE" \
                --db "$HOOKSTORM_HISTORY_DB"
            fi
          elif is_trusted_main_run; then
            echo "No baseline exists yet; creating the initial main-branch baseline."
            hookstorm history export latest \
              --output "$HOOKSTORM_BASELINE_FILE" \
              --db "$HOOKSTORM_HISTORY_DB"
          else
            echo "::error::No main-branch baseline found. Run this workflow successfully on main first."
            exit 1
          fi

      - name: Save refreshed main-branch baseline
        if: >-
          success() &&
          github.ref == 'refs/heads/main' &&
          (github.event_name == 'push' || github.event_name == 'workflow_dispatch')
        uses: actions/cache/save@0057852bfaa89a56745cba8c7296529d2fc39830
        with:
          path: .hookstorm-baselines/
          key: hookstorm-baseline-main-${{{{ github.sha }}}}-${{{{ github.run_id }}}}

      - name: Upload JUnit report
        if: always()
        uses: actions/upload-artifact@cf430e030ddbb5b0abf93d22962f4752f3646cd9
        with:
          name: hookstorm-webhook-regression
          path: test-results/*.xml
          if-no-files-found: warn
          retention-days: 14
"""


def initialize_project(
    *,
    root: str | Path = ".",
    scenario_path: str = "scenarios/webhook.yaml",
    workflow_path: str = ".github/workflows/webhook-regression.yml",
    event_type: str = "test.ping",
    event_id: str = "evt_hookstorm_init_001",
    signature_provider: str = "generic",
    include_workflow: bool = True,
    force: bool = False,
) -> list[Path]:
    """Create a starter scenario and optional GitHub Actions workflow."""
    scenario_rel = _relative_project_path(scenario_path, "scenario path")
    workflow_rel = _relative_project_path(workflow_path, "workflow path")
    if scenario_rel == workflow_rel:
        raise ValueError("Scenario and workflow paths must be different")
    if include_workflow and not workflow_rel.as_posix().startswith(".github/workflows/"):
        raise ValueError("workflow path must be inside .github/workflows/")
    if scenario_rel.suffix.lower() not in {".yaml", ".yml"}:
        raise ValueError("scenario path must end in .yaml or .yml")
    if include_workflow and workflow_rel.suffix.lower() not in {".yaml", ".yml"}:
        raise ValueError("workflow path must end in .yaml or .yml")

    project_root = Path(root).resolve()
    scenario_file = (project_root / scenario_rel).resolve()
    workflow_file = (project_root / workflow_rel).resolve()
    if not scenario_file.is_relative_to(project_root) or (
        include_workflow and not workflow_file.is_relative_to(project_root)
    ):
        raise ValueError("Generated files must stay inside the current project directory")
    outputs = [(scenario_file, render_scenario(
        event_type=event_type,
        event_id=event_id,
        signature_provider=signature_provider,
    ))]
    if include_workflow:
        outputs.append((
            workflow_file,
            render_workflow(
                scenario_path=scenario_rel.as_posix(),
                workflow_path=workflow_rel.as_posix(),
            ),
        ))

    existing = [path for path, _ in outputs if path.exists()]
    if existing and not force:
        names = ", ".join(str(path.relative_to(project_root)) for path in existing)
        raise FileExistsError(
            f"Refusing to overwrite existing file(s): {names}. Use --force to replace them."
        )

    written = []
    try:
        for path, contents in outputs:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(contents, encoding="utf-8")
            written.append(path)
    except OSError:
        if not force:
            for path in written:
                try:
                    path.unlink()
                except OSError:
                    pass
        raise
    return written
