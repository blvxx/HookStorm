"""Install HookStorm distributions and exercise each installed CLI in isolation."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import tempfile
import venv
from pathlib import Path


def _distribution_paths(
    value: str | None,
    project_root: Path,
    *,
    include_all: bool = False,
) -> list[Path]:
    if include_all:
        if value is not None:
            raise SystemExit("Do not pass a distribution path together with --all.")
        distributions: list[Path] = []
        for pattern, label in (
            ("hookstorm-*.whl", "wheel"),
            ("hookstorm-*.tar.gz", "source distribution"),
        ):
            matches = sorted((project_root / "dist").glob(pattern))
            if len(matches) != 1:
                raise SystemExit(
                    f"Expected exactly one HookStorm {label} in dist/, found {len(matches)}."
                )
            distributions.append(matches[0].resolve())
        return distributions

    if value is None:
        matches = sorted((project_root / "dist").glob("hookstorm-*.whl"))
        if len(matches) != 1:
            raise SystemExit(
                "Expected exactly one HookStorm wheel in dist/, "
                f"found {len(matches)}. Pass a wheel or source archive explicitly."
            )
        distribution = matches[0]
    else:
        distribution = Path(value).expanduser()

    distribution = distribution.resolve()
    if not distribution.is_file():
        raise SystemExit(f"HookStorm distribution does not exist: {distribution}")
    if not (distribution.name.endswith(".whl") or distribution.name.endswith(".tar.gz")):
        raise SystemExit(
            "Expected a HookStorm wheel (.whl) or source distribution (.tar.gz), "
            f"got: {distribution.name}"
        )
    return [distribution]


def _smoke_distribution(distribution: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="hookstorm-package-smoke-") as temporary:
        environment = Path(temporary) / "venv"
        venv.EnvBuilder(with_pip=True).create(environment)

        scripts = environment / ("Scripts" if os.name == "nt" else "bin")
        python = scripts / ("python.exe" if os.name == "nt" else "python")
        cli = scripts / ("hookstorm.exe" if os.name == "nt" else "hookstorm")

        subprocess.run(
            [str(python), "-m", "pip", "install", str(distribution)],
            check=True,
        )
        subprocess.run([str(cli), "--version"], check=True, cwd=temporary)
        exported_schema = subprocess.run(
            [str(cli), "schema"],
            check=True,
            cwd=temporary,
            capture_output=True,
            text=True,
        )
        schema_document = json.loads(exported_schema.stdout)
        if schema_document.get("$id") != "urn:hookstorm:scenario:v1":
            raise SystemExit(
                f"Installed HookStorm CLI did not expose its packaged JSON Schema for {distribution.name}"
            )
        subprocess.run(
            [
                str(cli),
                "init",
                "--no-workflow",
                "--signature-provider",
                "generic",
                "--scenario-path",
                "smoke-scenario.yaml",
            ],
            check=True,
            cwd=temporary,
        )

        scenario = Path(temporary) / "smoke-scenario.yaml"
        if not scenario.is_file():
            raise SystemExit(
                f"Installed HookStorm CLI did not create its starter scenario for {distribution.name}"
            )

        subprocess.run(
            [str(cli), "validate", scenario.name],
            check=True,
            cwd=temporary,
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Install HookStorm distributions and smoke-test the installed CLI."
    )
    parser.add_argument(
        "distribution",
        nargs="?",
        help="wheel or source archive to install (defaults to the sole wheel in dist/)",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="smoke-test exactly one wheel and one source distribution from dist/",
    )
    args = parser.parse_args(argv)

    project_root = Path(__file__).resolve().parents[1]
    distributions = _distribution_paths(
        args.distribution,
        project_root,
        include_all=args.all,
    )
    for distribution in distributions:
        print(f"Smoke-testing {distribution.name}")
        _smoke_distribution(distribution)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
