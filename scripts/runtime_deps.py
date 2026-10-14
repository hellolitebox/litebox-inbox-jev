"""Whether two `pyproject.toml` files declare the same runtime dependencies.

    runtime_deps.py DEPLOYED BRANCH    exit 0 when they match, 1 (and what differs on stderr)
                                       when they don't, 2 when a file can't be read or parsed

scripts/check_branch.sh mounts a branch's code into the deployed image, whose packages are the
deployment's. That only holds while the branch needs the same packages: the same
`requires-python`, `dependencies` and optional-dependency groups, `dev` excepted (the image never
installs it). Tool config (`[tool.ruff]`, `[tool.mypy]`, coverage…), the version, the scripts
and the build settings don't change what is installed, so this comparison ignores them. That
holds for `pyproject.toml` alone: the caller compares `uv.lock` first, and `uv.lock` records the
version and every `dev` specifier, so a branch that changes either is still refused there. Only
tool config passes end to end.

Standard library only: it runs with the host's `python3 -I` (3.11 or newer, for `tomllib`),
outside the app image.
"""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path
from typing import Any

# The optional-dependency group the image never installs (`uv sync --no-dev`, no extras).
DEV_EXTRA = "dev"


def runtime_spec(pyproject: dict[str, Any]) -> dict[str, Any]:
    """The parts of a parsed `pyproject.toml` that decide what the image installs."""
    project = pyproject.get("project", {})
    extras = project.get("optional-dependencies", {})
    return {
        "requires-python": project.get("requires-python"),
        "dependencies": project.get("dependencies", []),
        "optional-dependencies": {k: v for k, v in extras.items() if k != DEV_EXTRA},
    }


def differences(deployed: dict[str, Any], branch: dict[str, Any]) -> list[str]:
    """The `[project]` keys whose runtime value differs, in a fixed order."""
    old, new = runtime_spec(deployed), runtime_spec(branch)
    return [key for key in old if old[key] != new[key]]


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: runtime_deps.py DEPLOYED_PYPROJECT BRANCH_PYPROJECT", file=sys.stderr)
        return 2
    try:
        deployed, branch = (tomllib.loads(Path(path).read_text()) for path in argv)
    except (OSError, tomllib.TOMLDecodeError) as e:
        print(f"can't read pyproject.toml: {e}", file=sys.stderr)
        return 2
    changed = differences(deployed, branch)
    if changed:
        print(f"[project] runtime dependencies differ: {', '.join(changed)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
