from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from hookstorm.scaffold import HOOKSTORM_INSTALL_REF

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_PATH = ROOT / "examples" / "ci" / "webhook-regression.yml"


def _load_workflow() -> dict:
    import yaml

    return yaml.load(WORKFLOW_PATH.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)


def test_external_workflow_template_has_safe_triggers_and_permissions() -> None:
    workflow = _load_workflow()

    assert workflow["name"] == "Webhook regression"
    assert "push" in workflow["on"]
    assert "pull_request" in workflow["on"]
    assert "workflow_dispatch" in workflow["on"]
    assert workflow["permissions"]["contents"] == "read"

    job = workflow["jobs"]["webhook-regression"]
    assert job["environment"] == "staging"
    assert "github.event.pull_request.head.repo.full_name == github.repository" in job["if"]
    assert job["env"]["HOOKSTORM_TARGET_URL"] == "${{ vars.HOOKSTORM_TARGET_URL }}"
    assert job["env"]["HOOKSTORM_SECRET"] == "${{ secrets.HOOKSTORM_SECRET }}"
    assert job["env"]["HOOKSTORM_ALLOW_INSECURE_HTTP"] == (
        "${{ vars.HOOKSTORM_ALLOW_INSECURE_HTTP || '0' }}"
    )
    preflight = next(step for step in job["steps"] if step.get("name") == "Preflight staging endpoint")
    assert "--allow-insecure-http" in preflight["run"]
    run_step = next(step for step in job["steps"] if step.get("name") == "Run webhook scenario and compare with baseline")
    assert "--allow-insecure-http" in run_step["run"]
    workflow_text = WORKFLOW_PATH.read_text(encoding="utf-8")
    assert len(HOOKSTORM_INSTALL_REF) == 40
    assert all(character in "0123456789abcdef" for character in HOOKSTORM_INSTALL_REF)
    assert f"git+https://github.com/blvxx/HookStorm.git@{HOOKSTORM_INSTALL_REF}" in workflow_text
    assert "7ab9ffb6ab510b4067916a83068bf60f8c010a70" not in workflow_text

    step_uses = [step.get("uses", "") for step in job["steps"]]
    assert any("actions/cache/restore@" in value for value in step_uses)
    assert any("actions/cache/save@" in value for value in step_uses)


def test_external_workflow_bash_steps_pass_syntax_check() -> None:
    bash = shutil.which("bash")
    if bash is None:
        return

    workflow = _load_workflow()
    steps = workflow["jobs"]["webhook-regression"]["steps"]
    scripts = [step["run"] for step in steps if step.get("shell") == "bash"]

    assert scripts, "Expected at least one Bash run block"
    for script in scripts:
        checked = subprocess.run(
            [bash, "-n"],
            input=script,
            text=True,
            capture_output=True,
            check=False,
        )
        assert checked.returncode == 0, checked.stderr


def test_copy_ready_template_pins_all_github_actions_to_full_shas() -> None:
    import re

    workflow = _load_workflow()
    steps = workflow["jobs"]["webhook-regression"]["steps"]
    for step in steps:
        action = step.get("uses")
        if action is None or action.startswith("./"):
            continue
        reference = action.rsplit("@", 1)[-1].split()[0]
        assert re.fullmatch(r"[0-9a-f]{40}", reference), action


def test_latency_regression_gate_uses_relative_and_absolute_p95_thresholds() -> None:
    import yaml
    workflow_root = ROOT / ".github" / "workflows"
    builtin = yaml.load((workflow_root / "webhook-scenarios.yml").read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    run_step = next(step for step in builtin["jobs"]["scenarios"]["steps"] if step.get("name") == "Run and compare provider / delivery scenarios")
    assert "--latency-threshold-pct 20 --latency-min-delta-ms 25" in run_step["run"]
    assert "--latency-threshold-pct 1000" not in run_step["run"]
    external = _load_workflow()
    job = external["jobs"]["webhook-regression"]
    assert job["env"]["HOOKSTORM_LATENCY_MIN_DELTA_MS"] == "${{ vars.HOOKSTORM_LATENCY_MIN_DELTA_MS || '25' }}"
    compare_step = next(step for step in job["steps"] if step.get("name") == "Run webhook scenario and compare with baseline")
    assert "--latency-min-delta-ms" in compare_step["run"]