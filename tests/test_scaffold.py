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


@pytest.mark.parametrize(
    ("template", "provider", "event_type"),
    [
        ("generic", "generic", "test.ping"),
        ("stripe", "stripe", "payment_intent.succeeded"),
        ("github", "github", "issues.opened"),
        ("shopify", "shopify", "orders/create"),
        ("standard-webhooks", "standard_webhooks", "invoice.created"),
    ],
)
def test_initialize_project_templates_create_valid_provider_payloads(
    tmp_path, template: str, provider: str, event_type: str
) -> None:
    initialize_project(root=tmp_path, template=template, include_workflow=False)

    scenario = load_scenario(tmp_path / "scenarios" / "webhook.yaml")
    event = scenario["events"][0]
    assert scenario["delivery"]["signature_provider"] == provider
    assert event["id"] == event_type
    assert isinstance(event["payload"], dict)
    assert event["payload"]
    if template == "github":
        assert event["headers"]["X-GitHub-Event"] == "issues"
        assert event["headers"]["X-GitHub-Delivery"] == event["event_id"]
    if template == "shopify":
        assert event["headers"]["X-Shopify-Topic"] == "orders/create"
        assert event["headers"]["X-Shopify-Webhook-Id"] == event["event_id"]


def test_update_workflow_install_ref_changes_only_the_hookstorm_pin(tmp_path) -> None:
    from hookstorm.scaffold import update_workflow_install_ref

    initialize_project(root=tmp_path)
    workflow_path = tmp_path / ".github" / "workflows" / "webhook-regression.yml"
    original = workflow_path.read_text(encoding="utf-8")
    new_ref = "a" * 40

    assert update_workflow_install_ref(workflow_path, install_ref=new_ref) is True
    updated = workflow_path.read_text(encoding="utf-8")
    assert updated == original.replace(HOOKSTORM_INSTALL_REF, new_ref)
    assert update_workflow_install_ref(workflow_path, install_ref=new_ref) is False


def test_update_workflow_install_ref_rejects_unrecognized_workflows(tmp_path) -> None:
    from hookstorm.scaffold import update_workflow_install_ref

    workflow_path = tmp_path / "custom.yml"
    workflow_path.write_text("name: custom workflow\n", encoding="utf-8")

    with pytest.raises(ValueError, match="exactly one pinned HookStorm install URL"):
        update_workflow_install_ref(workflow_path)


def test_initialize_project_creates_gitlab_ci_config_on_default_branch_only(tmp_path) -> None:
    written = initialize_project(root=tmp_path, ci_provider="gitlab")

    scenario_path = tmp_path / "scenarios" / "webhook.yaml"
    workflow_path = tmp_path / ".gitlab-ci.yml"
    assert scenario_path in written
    assert workflow_path in written
    scenario = load_scenario(scenario_path)
    assert scenario["events"][0]["id"] == "test.ping"

    workflow = yaml.load(workflow_path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    assert workflow["workflow"]["rules"][0]["if"] == (
        "$CI_COMMIT_BRANCH == $CI_DEFAULT_BRANCH"
    )
    assert workflow["workflow"]["rules"][1]["when"] == "never"
    job = workflow["webhook-regression"]
    assert job["image"] == "python:3.12-slim"
    assert job["resource_group"] == "hookstorm-webhook-regression-baseline"
    assert job["environment"]["name"] == "staging"
    assert ".hookstorm-baselines/" in job["cache"]["paths"]
    script = job["script"][0]
    assert "HOOKSTORM_TARGET_URL" in script
    assert '--target "$HOOKSTORM_TARGET_URL"' in script
    assert "hookstorm doctor" in script
    assert "hookstorm run" in script
    assert "history compare-file" in script
    assert "history export latest" in script
    assert "junit: test-results/webhook-regression.xml" in workflow_path.read_text(
        encoding="utf-8"
    )
    assert f"git+https://github.com/blvxx/HookStorm.git@{HOOKSTORM_INSTALL_REF}" in script


def test_generated_gitlab_script_has_valid_shell_syntax(tmp_path) -> None:
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash is not installed on this runner")

    initialize_project(root=tmp_path, ci_provider="gitlab")
    workflow = yaml.load(
        (tmp_path / ".gitlab-ci.yml").read_text(encoding="utf-8"),
        Loader=yaml.BaseLoader,
    )
    script = workflow["webhook-regression"]["script"][0]
    result = subprocess.run(
        [bash, "-n"],
        input=script,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_gitlab_ci_requires_root_config_path(tmp_path) -> None:
    with pytest.raises(ValueError, match="project root as .gitlab-ci.yml"):
        initialize_project(
            root=tmp_path,
            ci_provider="gitlab",
            workflow_path="ci/gitlab.yml",
        )


def test_update_workflow_install_ref_supports_gitlab_config(tmp_path) -> None:
    from hookstorm.scaffold import update_workflow_install_ref

    initialize_project(root=tmp_path, ci_provider="gitlab")
    workflow_path = tmp_path / ".gitlab-ci.yml"
    original = workflow_path.read_text(encoding="utf-8")
    new_ref = "c" * 40

    assert update_workflow_install_ref(workflow_path, install_ref=new_ref) is True
    updated = workflow_path.read_text(encoding="utf-8")
    assert updated == original.replace(HOOKSTORM_INSTALL_REF, new_ref)


def test_provider_delivery_headers_follow_an_overridden_event_id(tmp_path) -> None:
    initialize_project(
        root=tmp_path,
        template="shopify",
        event_id="evt_custom_shopify_001",
        include_workflow=False,
    )

    event = load_scenario(tmp_path / "scenarios" / "webhook.yaml")["events"][0]
    assert event["event_id"] == "evt_custom_shopify_001"
    assert event["headers"]["X-Shopify-Webhook-Id"] == "evt_custom_shopify_001"


def test_provider_headers_follow_custom_event_types(tmp_path) -> None:
    initialize_project(
        root=tmp_path,
        template="github",
        event_type="pull_request.opened",
        include_workflow=False,
    )

    event = load_scenario(tmp_path / "scenarios" / "webhook.yaml")["events"][0]
    assert event["id"] == "pull_request.opened"
    assert event["headers"]["X-GitHub-Event"] == "pull_request"
