"""Shared helpers for invoking `uv pip compile` against this repo's requirements files.

Split out from update_pins.py/regenerate.py so both can use it without one
importing the other.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Sequence

import uv

REPO_ROOT = Path(__file__).resolve().parents[3]
PYPROJECT_DIR = Path(__file__).resolve().parent / "not-a-project"
CONSTRAINTS_PATH = REPO_ROOT / "tools" / "constraints.txt"


def find_input_files() -> Sequence[Path]:
    """Every requirements*.txt file under tools/, excluding vendored third_party code."""
    return sorted(
        path for path in (REPO_ROOT / "tools").rglob("requirements*.txt")
        if "third_party" not in path.parts
    )


def compile_constraints() -> None:
    input_files = find_input_files()
    subprocess.run(
        [
            uv.find_uv_bin(), "pip", "compile",
            "--project", str(PYPROJECT_DIR.relative_to(REPO_ROOT)),
            "-o", str(CONSTRAINTS_PATH.relative_to(REPO_ROOT)),
            *(str(path.relative_to(REPO_ROOT)) for path in input_files),
        ],
        cwd=REPO_ROOT,
        check=True,
    )
