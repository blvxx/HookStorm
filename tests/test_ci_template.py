from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

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
    assert "b163b2a4af28176bae5442eeb4e6c41e7ea326cb" in WORKFLOW_PATH.read_text(encoding="utf-8")
    assert "7ab9ffb6ab510b4067916a83068bf60f8c010a70" not in WORKFLOW_PATH.read_text(encoding="utf-8")

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
