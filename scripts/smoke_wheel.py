"""Install the built wheel into a clean virtual environment and smoke-test its CLI."""

from __future__ import annotations

import os
import subprocess
import tempfile
import venv
from pathlib import Path


def main() -> int:
    project_root = Path(__file__).resolve().parents[1]
    wheels = sorted((project_root / "dist").glob("hookstorm-*.whl"))
    if len(wheels) != 1:
        raise SystemExit(
            f"Expected exactly one HookStorm wheel in dist/, found {len(wheels)}"
        )

    with tempfile.TemporaryDirectory(prefix="hookstorm-wheel-smoke-") as temporary:
        environment = Path(temporary) / "venv"
        venv.EnvBuilder(with_pip=True).create(environment)

        scripts = environment / ("Scripts" if os.name == "nt" else "bin")
        python = scripts / ("python.exe" if os.name == "nt" else "python")
        cli = scripts / ("hookstorm.exe" if os.name == "nt" else "hookstorm")

        subprocess.run(
            [str(python), "-m", "pip", "install", str(wheels[0])],
            check=True,
        )
        subprocess.run([str(cli), "--version"], check=True, cwd=temporary)
        subprocess.run(
            [
                str(cli),
                "validate",
                str(project_root / "examples" / "standard-webhooks.yaml"),
            ],
            check=True,
            cwd=temporary,
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
