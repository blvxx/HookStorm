from __future__ import annotations

import shutil
import subprocess

import pytest
import yaml

from hookstorm.config import load_scenario
from hookstorm.scaffold import HOOKSTORM_INSTALL_REF, initialize_project


def test_initialize_project_creates_valid_scenario_and_workflow(tmp_path) -> None:
    written = initialize_project(
        root=tmp_path,
        event_type="invoice.created",
        event_id="evt_invoice_created_001",
        signature_provider="github",
    )

    scenario_path = tmp_path / "scenarios" / "webhook.yaml"
    workflow_path = tmp_path / ".github" / "workflows" / "webhook-regression.yml"
    assert scenario_path in written
    assert workflow_path in written

    scenario = load_scenario(scenario_path)
    assert scenario["events"][0]["id"] == "invoice.created"
    assert scenario["events"][0]["event_id"] == "evt_invoice_created_001"
    assert scenario["delivery"]["signature_provider"] == "github"

    workflow = yaml.load(workflow_path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    env = workflow["jobs"]["webhook-regression"]["env"]
    assert env["HOOKSTORM_SCENARIO"] == (
        "${{ vars.HOOKSTORM_SCENARIO || 'scenarios/webhook.yaml' }}"
    )
    assert env["HOOKSTORM_TARGET_URL"] == "${{ vars.HOOKSTORM_TARGET_URL }}"
    assert env["HOOKSTORM_SECRET"] == "${{ secrets.HOOKSTORM_SECRET }}"
    assert env["HOOKSTORM_ALLOW_INSECURE_HTTP"] == (
        "${{ vars.HOOKSTORM_ALLOW_INSECURE_HTTP || '0' }}"
    )
    workflow_text = workflow_path.read_text(encoding="utf-8")
    steps = workflow["jobs"]["webhook-regression"]["steps"]
    for step in steps:
        action = step.get("uses")
        if action is not None and not action.startswith("./"):
            reference = action.rsplit("@", 1)[-1].split()[0]
            assert len(reference) == 40
            assert all(character in "0123456789abcdef" for character in reference)
    preflight = next(step for step in steps if step.get("name") == "Preflight staging endpoint")
    assert "hookstorm doctor" in preflight["run"]
    assert "--connect" in preflight["run"]
    assert "--allow-insecure-http" in preflight["run"]
    assert HOOKSTORM_INSTALL_REF == "660240e1fe1b612da6ca64c2cf17f6daf0c9bac2"
    assert len(HOOKSTORM_INSTALL_REF) == 40
    assert all(character in "0123456789abcdef" for character in HOOKSTORM_INSTALL_REF)
    assert f"git+https://github.com/blvxx/HookStorm.git@{HOOKSTORM_INSTALL_REF}" in workflow_text
    assert "--allow-insecure-http" in workflow_text
    assert "HOOKSTORM_ALLOW_INSECURE_HTTP" in workflow_text
    assert "7ab9ffb6ab510b4067916a83068bf60f8c010a70" not in workflow_text
    assert "8755dc158c732a420e09b83d8bae0e29a10e9206" not in workflow_text
    assert "https://example" not in workflow_text


def test_initialize_project_supports_standard_webhooks(tmp_path) -> None:
    initialize_project(
        root=tmp_path,
        signature_provider="standard_webhooks",
    )

    scenario = load_scenario(tmp_path / "scenarios" / "webhook.yaml")
    assert scenario["delivery"]["signature_provider"] == "standard_webhooks"


def test_generated_workflow_bash_blocks_have_valid_syntax(tmp_path) -> None:
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash is not installed on this runner")

    initialize_project(root=tmp_path)
    workflow = yaml.load(
        (tmp_path / ".github/workflows/webhook-regression.yml").read_text(encoding="utf-8"),
        Loader=yaml.BaseLoader,
    )
    steps = workflow["jobs"]["webhook-regression"]["steps"]
    scripts = [step["run"] for step in steps if step.get("shell") == "bash"]

    assert scripts
    for script in scripts:
        result = subprocess.run(
            [bash, "-n"],
            input=script,
            text=True,
            capture_output=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr


def test_initialize_refuses_existing_files_without_partial_writes(tmp_path) -> None:
    scenario_path = tmp_path / "scenarios" / "webhook.yaml"
    scenario_path.parent.mkdir(parents=True)
    scenario_path.write_text("keep-my-config", encoding="utf-8")

    with pytest.raises(FileExistsError, match="--force"):
        initialize_project(root=tmp_path)

    workflow_path = tmp_path / ".github" / "workflows" / "webhook-regression.yml"
    assert scenario_path.read_text(encoding="utf-8") == "keep-my-config"
    assert not workflow_path.exists()


def test_initialize_force_replaces_both_generated_files(tmp_path) -> None:
    initialize_project(root=tmp_path)
    scenario_path = tmp_path / "scenarios" / "webhook.yaml"
    workflow_path = tmp_path / ".github" / "workflows" / "webhook-regression.yml"
    scenario_path.write_text("old-scenario", encoding="utf-8")
    workflow_path.write_text("old-workflow", encoding="utf-8")

    initialize_project(
        root=tmp_path,
        signature_provider="stripe",
        force=True,
    )

    assert "signature_provider: stripe" in scenario_path.read_text(encoding="utf-8")
    assert "old-workflow" not in workflow_path.read_text(encoding="utf-8")


@pytest.mark.parametrize("path", ["../outside.yaml", "/tmp/outside.yaml", "C:/outside.yaml"])
def test_initialize_rejects_paths_outside_project(tmp_path, path: str) -> None:
    with pytest.raises(ValueError, match="relative path|inside the current project"):
        initialize_project(root=tmp_path, scenario_path=path)


def test_initialize_supports_scenario_only(tmp_path) -> None:
    written = initialize_project(root=tmp_path, include_workflow=False)

    assert written == [tmp_path / "scenarios" / "webhook.yaml"]
    assert not (tmp_path / ".github" / "workflows" / "webhook-regression.yml").exists()


def test_initialize_project_supports_retry_and_duplicate_options(tmp_path) -> None:
    initialize_project(root=tmp_path, max_attempts=3, duplicate_count=2)

    scenario = load_scenario(tmp_path / "scenarios" / "webhook.yaml")

    assert scenario["retry"]["max_attempts"] == 3
    assert scenario["retry"]["backoff_ms"] == 100
    assert scenario["delivery"]["duplicate_count"] == 2


@pytest.mark.parametrize(
    ("max_attempts", "duplicate_count"),
    [(0, 0), (11, 0), (True, 0), (1, -1), (1, 101), (1, False)],
)
def test_initialize_project_rejects_invalid_retry_and_duplicate_options(
    tmp_path, max_attempts: object, duplicate_count: object
) -> None:
    with pytest.raises(ValueError):
        initialize_project(
            root=tmp_path,
            include_workflow=False,
            max_attempts=max_attempts,
            duplicate_count=duplicate_count,
        )
