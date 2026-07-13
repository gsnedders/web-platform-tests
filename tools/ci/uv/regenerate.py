#!/usr/bin/env python3
"""Regenerate tools/constraints.txt and the pyproject.toml that drives it.

tools/ci/uv/not-a-project/pyproject.toml isn't a real Python project; it
exists solely to hold the `[tool.uv]` settings - `required-environments`
below, and `[tool.uv.pip] universal = true` - that `uv pip compile` needs
to resolve tools/constraints.txt across every platform and Python version
this project supports. `uv pip compile --project` discovers a pyproject.toml
by directory rather than by name, which is what makes stashing it away here
possible.

This does two things, in order:

1. Regenerates that pyproject.toml's `required-environments` list from
   PYTHON_VERSIONS and PLATFORMS below.
2. Re-runs `uv pip compile` against every requirements*.txt file under
   tools/ (see find_input_files() in compile.py), to regenerate it.
"""

from __future__ import annotations

import itertools
from pathlib import Path
from typing import Mapping, Optional, Sequence

from ...wpt import virtualenv
from .compile import PYPROJECT_DIR, compile_constraints

PYPROJECT_PATH = PYPROJECT_DIR / "pyproject.toml"

PYTHON_VERSIONS: Sequence[str] = ["3.9", "3.10", "3.11", "3.12", "3.13", "3.14"]

PLATFORMS: Sequence[Mapping[str, str]] = [
    {"sys_platform": "darwin", "platform_machine": "x86_64"},
    {"sys_platform": "darwin", "platform_machine": "arm64"},
    {"sys_platform": "win32", "platform_machine": "AMD64"},
    {"sys_platform": "linux", "platform_machine": "x86_64"},
]


def build_environments() -> Sequence[str]:
    environments = []
    for platform, python_version in itertools.product(PLATFORMS, PYTHON_VERSIONS):
        clauses = [
            f"sys_platform == '{platform['sys_platform']}'",
            f"platform_machine == '{platform['platform_machine']}'",
            "implementation_name == 'cpython'",
            f"python_version == '{python_version}'",
        ]
        environments.append(" and ".join(clauses))
    return environments


def render_required_environments(environments: Sequence[str]) -> str:
    lines = ["required-environments = ["]
    lines.extend(f'    "{environment}",' for environment in environments)
    lines.append("]")
    return "\n".join(lines)


def update_pyproject_toml(path: Path) -> None:
    text = path.read_text()

    start = text.index("required-environments = [")
    end = text.index("]", start) + 1

    new_text = text[:start] + render_required_environments(build_environments()) + text[end:]
    path.write_text(new_text)


def run(venv: Optional[virtualenv.Virtualenv]) -> None:
    update_pyproject_toml(PYPROJECT_PATH)
    compile_constraints()


if __name__ == "__main__":
    run(None)
