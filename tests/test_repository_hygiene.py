from __future__ import annotations

import tomllib
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_package_metadata_has_author_documentation_and_dev_tooling() -> None:
    project_file = ROOT / "pyproject.toml"
    data = tomllib.loads(project_file.read_text(encoding="utf-8"))
    project = data["project"]

    assert project["authors"] == [
        {"name": "Артём Калинин", "email": "enotzz@bk.ru"}
    ]
    assert "Documentation" in project["urls"]
    assert "PyPI" not in project["urls"]
    dev_dependencies = project["optional-dependencies"]["dev"]
    assert any(item.startswith("mypy") for item in dev_dependencies)
    assert any(item.startswith("coverage") for item in dev_dependencies)
    assert project["optional-dependencies"]["pytest"] == ["pytest>=8.0,<10"]
    assert (ROOT / "src" / "hookstorm" / "py.typed").is_file()
    assert "py.typed" in data["tool"]["setuptools"]["package-data"]["hookstorm"]


def test_readme_is_a_short_project_landing_page() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    assert len(readme.splitlines()) < 180
    assert "30 seconds to first result" in readme
    assert "How is HookStorm different?" in readme
    assert "## Next" in readme
    assert "## Roadmap" not in readme
    assert "whsec_" not in readme
    assert "\\\\" not in readme
    webhook_workflow = (ROOT / ".github" / "workflows" / "webhook-scenarios.yml").read_text(encoding="utf-8")
    assert "--latency-threshold-pct 20 --latency-min-delta-ms 25" in webhook_workflow
    assert "--latency-threshold-pct 1000" not in webhook_workflow


def test_changelog_keeps_unreleased_changes_in_one_consistent_format() -> None:
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")

    assert changelog.count("## [Unreleased]") == 1
    assert "## 0.1.0a1" not in changelog
    for section in ("### Added", "### Changed", "### Fixed", "### Security"):
        assert section in changelog


def test_security_policy_has_a_private_reporting_route_and_exact_url_redaction() -> None:
    security = (ROOT / "SECURITY.md").read_text(encoding="utf-8")

    assert "latest published version only" in security
    assert "when available" not in security.lower()
    assert "mailto:enotzz@bk.ru" in security
    for detail in ("scheme", "hostname", "explicit port", "path", "query strings", "fragments"):
        assert detail in security


def test_ci_and_release_gates_run_mypy_and_coverage() -> None:
    for relative_path in (".github/workflows/ci.yml", ".github/workflows/release.yml"):
        workflow = (ROOT / relative_path).read_text(encoding="utf-8")
        assert "run: mypy" in workflow
        assert "coverage run -m pytest" in workflow
        assert "run: coverage report" in workflow

def test_ci_uploads_pytest_artifact_only_when_report_exists() -> None:
    workflow_path = ROOT / ".github" / "workflows" / "ci.yml"
    workflow = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))
    steps = workflow["jobs"]["test"]["steps"]
    upload = next(step for step in steps if step.get("name") == "Upload pytest JUnit report")

    assert upload["if"] == "always() && hashFiles('test-results/pytest.xml') != ''"
    assert upload["with"]["if-no-files-found"] == "error"
