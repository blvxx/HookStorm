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
