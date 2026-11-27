from __future__ import annotations

import tomllib
from pathlib import Path

from hookstorm import __version__


def test_runtime_version_matches_project_metadata() -> None:
    project = tomllib.loads(
        (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(
            encoding="utf-8"
        )
    )["project"]

    assert project["version"] == __version__
    assert project["version"] == "0.1.0a1"
    assert "Development Status :: 3 - Alpha" in project["classifiers"]
    assert "Programming Language :: Python :: 3.14" in project["classifiers"]
