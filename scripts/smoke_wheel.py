"""Install a built HookStorm distribution and exercise its installed CLI in isolation."""

from __future__ import annotations

import argparse
import os
import subprocess
import tempfile
import venv
from pathlib import Path


def _distribution_path(value: str | None, project_root: Path) -> Path:
    if value is None:
        distributions = sorted((project_root / "dist").glob("hookstorm-*.whl"))
        if len(distributions) != 1:
            raise SystemExit(
                "Expected exactly one HookStorm wheel in dist/, "
                f"found {len(distributions)}. Pass a wheel or source archive explicitly."
            )
        distribution = distributions[0]
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
    return distribution


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Install a HookStorm wheel or source archive and smoke-test its CLI."
    )
    parser.add_argument(
        "distribution",
        nargs="?",
        help="wheel or source distribution to install (defaults to the sole wheel in dist/)",
    )
    args = parser.parse_args(argv)

    project_root = Path(__file__).resolve().parents[1]
    distribution = _distribution_path(args.distribution, project_root)

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
            raise SystemExit("Installed HookStorm CLI did not create its starter scenario")

        subprocess.run(
            [str(cli), "validate", scenario.name],
            check=True,
            cwd=temporary,
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
