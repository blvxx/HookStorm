from __future__ import annotations

from pathlib import Path

import yaml


def test_release_workflow_only_publishes_after_a_published_release() -> None:
    workflow_path = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "release.yml"
    workflow = yaml.load(workflow_path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)

    assert workflow["on"]["release"]["types"] == ["published"]
    assert "workflow_dispatch" in workflow["on"]
    assert workflow["jobs"]["build"]["steps"][4]["if"] == "github.event_name == 'release'"
    assert workflow["jobs"]["publish"]["needs"] == "build"
    assert workflow["jobs"]["publish"]["if"] == (
        "github.event_name == 'release' && github.event.action == 'published'"
    )


def test_only_pypi_publish_job_has_oidc_permission() -> None:
    workflow_path = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "release.yml"
    workflow = yaml.load(workflow_path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)

    assert workflow["permissions"] == {"contents": "read"}
    assert workflow["jobs"]["build"]["permissions"] == {"contents": "read"}
    assert workflow["jobs"]["publish"]["permissions"] == {"id-token": "write"}


def test_release_workflow_actions_are_pinned_to_full_commit_shas() -> None:
    workflow_path = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "release.yml"
    workflow_text = workflow_path.read_text(encoding="utf-8")
    workflow = yaml.load(workflow_text, Loader=yaml.BaseLoader)

    action_uses = [
        step["uses"]
        for job in workflow["jobs"].values()
        for step in job["steps"]
        if "uses" in step
    ]
    assert action_uses
    for action in action_uses:
        reference = action.rsplit("@", 1)[1].split()[0]
        assert len(reference) == 40
        assert all(character in "0123456789abcdef" for character in reference)



def test_release_commit_must_be_reachable_from_main() -> None:
    workflow_path = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "release.yml"
    workflow = yaml.load(workflow_path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    steps = workflow["jobs"]["build"]["steps"]

    checkout = steps[0]
    assert checkout["with"]["fetch-depth"] == "0"
    assert checkout["with"]["ref"] == (
        "${{ github.event_name == 'release' && github.event.release.tag_name || github.ref }}"
    )

    ancestry_check = next(
        step for step in steps
        if step.get("name") == "Verify release commit is reachable from main"
    )
    assert ancestry_check["if"] == "github.event_name == 'release'"
    assert "git merge-base --is-ancestor" in ancestry_check["run"]
    assert "FETCH_HEAD" in ancestry_check["run"]



def test_release_workflow_smoke_tests_wheel_and_source_distribution() -> None:
    workflow_path = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "release.yml"
    workflow = yaml.load(workflow_path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    steps = workflow["jobs"]["build"]["steps"]

    smoke_test = next(
        step for step in steps
        if step.get("name") == "Smoke-test wheel and source distribution"
    )
    script = smoke_test["run"]
    assert script == "python scripts/smoke_wheel.py --all"
    assert "examples/" not in script

    package_smoke = (
        Path(__file__).resolve().parents[1] / "scripts" / "smoke_wheel.py"
    ).read_text(encoding="utf-8")
    assert '"init"' in package_smoke
    assert '"--no-workflow"' in package_smoke
    assert '"validate"' in package_smoke
    assert '"--all"' in package_smoke
    assert "smoke-scenario.yaml" in package_smoke
    assert "examples/" not in package_smoke

    smoke_test_index = steps.index(smoke_test)
    upload_index = next(
        index for index, step in enumerate(steps)
        if step.get("name") == "Upload release distributions"
    )
    assert smoke_test_index < upload_index



def test_ci_builds_and_smoke_tests_wheel_and_source_distribution() -> None:
    workflow_path = (
        Path(__file__).resolve().parents[1] / ".github" / "workflows" / "ci.yml"
    )
    workflow = yaml.load(workflow_path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    steps = workflow["jobs"]["test"]["steps"]

    build = next(step for step in steps if step.get("name") == "Build wheel and source distribution")
    smoke = next(
        step
        for step in steps
        if step.get("name") == "Smoke-test installed wheel and source distribution"
    )

    assert build["run"] == "python -m build --sdist --wheel --outdir dist"
    assert smoke["run"] == "python scripts/smoke_wheel.py --all"
    assert steps.index(build) < steps.index(smoke)


def test_all_repository_workflow_actions_are_pinned_to_full_commit_shas() -> None:
    import re

    workflow_dir = Path(__file__).resolve().parents[1] / ".github" / "workflows"
    workflow_paths = sorted((*workflow_dir.glob("*.yml"), *workflow_dir.glob("*.yaml")))
    assert workflow_paths

    for workflow_path in workflow_paths:
        workflow = yaml.load(workflow_path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
        for job_name, job in workflow.get("jobs", {}).items():
            for step in job.get("steps", []):
                action = step.get("uses")
                if action is None or action.startswith("./"):
                    continue
                reference = action.rsplit("@", 1)[-1].split()[0]
                assert re.fullmatch(r"[0-9a-f]{40}", reference), (
                    f"{workflow_path.relative_to(workflow_dir.parent.parent)} "
                    f"job {job_name!r} uses an unpinned action: {action}"
                )


def test_repository_workflows_do_not_persist_checkout_credentials() -> None:
    workflow_dir = Path(__file__).resolve().parents[1] / ".github" / "workflows"
    workflow_paths = sorted((*workflow_dir.glob("*.yml"), *workflow_dir.glob("*.yaml")))

    checkouts = []
    for workflow_path in workflow_paths:
        workflow = yaml.load(
            workflow_path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader
        )
        for job in workflow.get("jobs", {}).values():
            checkouts.extend(
                step
                for step in job.get("steps", [])
                if step.get("uses", "").startswith("actions/checkout@")
            )

    assert checkouts
    for checkout in checkouts:
        assert checkout.get("with", {}).get("persist-credentials") == "false"



def test_published_release_attaches_validated_distributions_with_limited_permissions() -> None:
    workflow_path = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "release.yml"
    workflow = yaml.load(workflow_path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)

    attach = workflow["jobs"]["attach"]
    assert attach["if"] == "github.event_name == 'release' && github.event.action == 'published'"
    assert attach["needs"] == "build"
    assert attach["permissions"] == {"contents": "write"}

    download = next(step for step in attach["steps"] if step.get("name") == "Download validated distributions")
    assert download["uses"] == "actions/download-artifact@37930b1c2abaa49bbe596cd826c3c89aef350131"
    assert download["with"] == {"name": "python-distributions", "path": "dist/"}

    upload = next(step for step in attach["steps"] if step.get("name") == "Attach wheel and source archive")
    assert upload["env"]["GH_TOKEN"] == "${{ github.token }}"
    assert upload["env"]["RELEASE_TAG"] == "${{ github.event.release.tag_name }}"
    assert upload["run"] == 'gh release upload "$RELEASE_TAG" dist/* --repo "$GITHUB_REPOSITORY"'
    assert "--clobber" not in upload["run"]

    assert workflow["jobs"]["publish"]["permissions"] == {"id-token": "write"}

def test_build_backend_requires_patched_setuptools() -> None:
    import tomllib

    project_path = Path(__file__).resolve().parents[1] / "pyproject.toml"
    project = tomllib.loads(project_path.read_text(encoding="utf-8"))
    assert "setuptools>=84.1.0" in project["build-system"]["requires"]
