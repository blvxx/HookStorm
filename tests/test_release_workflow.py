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


def test_ci_and_release_workflows_enforce_ruff_formatting() -> None:
    root = Path(__file__).resolve().parents[1] / ".github" / "workflows"
    for filename in ("ci.yml", "release.yml"):
        workflow = yaml.load((root / filename).read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
        steps = workflow["jobs"]["test" if filename == "ci.yml" else "build"]["steps"]
        format_step = next(step for step in steps if step.get("name") == "Format")
        assert format_step["run"] == "ruff format --check ."
