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
    assert "## Help validate the alpha" in readme
    assert "docs/user-validation.md" in readme
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


def test_optional_framework_and_docs_extras_remain_separate_from_core() -> None:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    extras = data["project"]["optional-dependencies"]

    assert extras["fastapi"] == ["fastapi>=0.115,<1"]
    assert extras["flask"] == ["Flask>=3.0,<4"]
    assert extras["docs"] == ["zensical>=0.1,<1"]
    core_dependencies = data["project"]["dependencies"]
    assert all("fastapi" not in item.lower() for item in core_dependencies)
    assert all("flask" not in item.lower() for item in core_dependencies)
    assert (ROOT / "src/hookstorm/integrations/_recorder.py").is_file()
    assert (ROOT / "src/hookstorm/integrations/fastapi.py").is_file()
    assert (ROOT / "src/hookstorm/integrations/flask.py").is_file()


def test_container_is_minimal_non_root_and_has_read_only_ci_smoke() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    dockerignore = (ROOT / ".dockerignore").read_text(encoding="utf-8")
    workflow = yaml.safe_load(
        (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    )

    assert "FROM python:3.12-slim AS builder" in dockerfile
    assert "FROM python:3.12-slim AS runtime" in dockerfile
    assert "USER 10001:10001" in dockerfile
    assert "ENTRYPOINT [\"hookstorm\"]" in dockerfile
    assert "USER root" not in dockerfile
    assert "tests" in dockerignore
    assert ".env" in dockerignore
    container_steps = workflow["jobs"]["container"]["steps"]
    assert any(step.get("name") == "Build minimal runtime image" for step in container_steps)
    assert any("os.getuid() != 0" in step.get("run", "") for step in container_steps)
    assert any("--read-only" in step.get("run", "") for step in container_steps)


def test_zensical_documentation_config_and_build_workflow_exist() -> None:
    config = (ROOT / "mkdocs.yml").read_text(encoding="utf-8")
    workflow = yaml.safe_load(
        (ROOT / ".github/workflows/docs.yml").read_text(encoding="utf-8")
    )
    docs_extra = tomllib.loads(
        (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )["project"]["optional-dependencies"]["docs"]

    assert docs_extra == ["zensical>=0.1,<1"]
    assert "site_name: HookStorm" in config
    assert "framework-integrations.md" in config
    assert "alpha-release-review.md" in config
    steps = workflow["jobs"]["build"]["steps"]
    assert any("zensical build" == step.get("run") for step in steps)
    assert any(".[docs]" in step.get("run", "") for step in steps)

def test_pages_deployment_is_main_only_and_uses_least_privilege_jobs() -> None:
    workflow = yaml.load(
        (ROOT / ".github/workflows/pages.yml").read_text(encoding="utf-8"),
        Loader=yaml.BaseLoader,
    )
    assert workflow["on"]["push"]["branches"] == ["main"]
    assert "pull_request" not in workflow["on"]
    assert workflow["permissions"] == {"contents": "read"}
    assert workflow["jobs"]["build"]["permissions"] == {"contents": "read"}
    assert workflow["jobs"]["deploy"]["needs"] == "build"
    assert workflow["jobs"]["deploy"]["permissions"] == {
        "pages": "write",
        "id-token": "write",
    }
    assert workflow["jobs"]["deploy"]["environment"]["name"] == "github-pages"
    assert workflow["concurrency"] == {
        "group": "github-pages",
        "cancel-in-progress": "false",
    }

    actions = [
        step["uses"]
        for job in workflow["jobs"].values()
        for step in job["steps"]
        if "uses" in step
    ]
    assert "actions/configure-pages@45bfe0192ca1faeb007ade9deae92b16b8254a0d" in actions
    assert "actions/upload-pages-artifact@fc324d3547104276b827a68afc52ff2a11cc49c9" in actions
    assert "actions/deploy-pages@368f82528645a54fb793d4d04e342629a3f51346" in actions
    assert any(step.get("run") == "zensical build" for step in workflow["jobs"]["build"]["steps"])
