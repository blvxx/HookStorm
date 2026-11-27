from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_composite_action_uses_full_sha_pinned_setup_python() -> None:
    action = yaml.safe_load(
        (ROOT / ".github" / "actions" / "run" / "action.yml").read_text(encoding="utf-8")
    )

    assert action["runs"]["using"] == "composite"
    setup = next(
        step for step in action["runs"]["steps"]
        if step.get("uses", "").startswith("actions/setup-python@")
    )
    assert re.fullmatch("[0-9a-f]{40}", setup["uses"].split("@", 1)[1])


def test_composite_action_keeps_remote_access_and_history_opt_in() -> None:
    action = yaml.safe_load(
        (ROOT / ".github" / "actions" / "run" / "action.yml").read_text(encoding="utf-8")
    )
    inputs = action["inputs"]
    assert inputs["allow-remote"]["default"] == "false"
    assert inputs["allow-insecure-http"]["default"] == "false"
    assert inputs["history-db"]["default"] == ""

    run_step = next(step for step in action["runs"]["steps"] if step.get("id") == "run")
    script = run_step["run"]
    assert "hookstorm validate" in script
    assert "hookstorm doctor" in script
    assert "run_args[@]" in script
    assert "--no-history" in script
    assert "allow-insecure-http requires allow-remote=true" in script


def test_consumer_example_pins_hookstorm_and_github_actions() -> None:
    workflow = (ROOT / "examples" / "ci" / "hookstorm-action.yml").read_text(encoding="utf-8")
    assert re.search(r"blvxx/HookStorm/.github/actions/run@[0-9a-f]{40}", workflow)
    assert re.search(r"actions/checkout@[0-9a-f]{40}", workflow)
    assert re.search(r"actions/upload-artifact@[0-9a-f]{40}", workflow)
    assert "HOOKSTORM_SECRET" in workflow
    assert "secrets.HOOKSTORM_SECRET" in workflow
