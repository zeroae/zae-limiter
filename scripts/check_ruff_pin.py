#!/usr/bin/env python3
"""Fail if the four ruff declarations in this repo disagree.

Four places name a ruff version, and every one of them formats or checks the same
files:

| Declaration                          | What it drives                              |
|--------------------------------------|---------------------------------------------|
| ``pyproject.toml`` ``[build-system]`` | the build hook that runs ``generate_sync.py``|
| ``pyproject.toml`` ``[dev]`` extra    | ``uv run ruff ...``, the documented command  |
| ``pyproject.toml`` hatch default env  | ``hatch run generate-sync``                  |
| ``.pre-commit-config.yaml`` ``rev``   | the commit hook and the CI lint job          |

(``.github/workflows/ci-lint.yml`` installs ruff for the ``verify-sync-generated``
hook; it is checked here too, so all five agree.)

Two different ruff versions produce two different answers on the same file. When the
generator formats with one and the hook checks with the other, the generated sync
twin is reported permanently out of date and the contributor has no way to see why —
observed on #513, filed as #486. This script is the check that makes that drift
impossible to commit.

Run directly, or via the ``check-ruff-pin`` pre-commit hook.
"""

from __future__ import annotations

import re
import sys
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = REPO_ROOT / "pyproject.toml"
PRE_COMMIT = REPO_ROOT / ".pre-commit-config.yaml"
CI_LINT = REPO_ROOT / ".github" / "workflows" / "ci-lint.yml"

# Every declaration must be an exact pin. A range would let `uv sync` and the hook
# resolve to different versions inside it, which is the bug this guards.
EXACT_PIN = re.compile(r"^ruff==(?P<version>\d+\.\d+\.\d+)$")


def _pin(requirement: str, where: str, errors: list[str]) -> str | None:
    match = EXACT_PIN.match(requirement.split("#")[0].strip())
    if match is None:
        errors.append(f"{where}: {requirement!r} is not an exact pin of the form ruff==X.Y.Z")
        return None
    return match.group("version")


def _find(requirements: list[str], where: str, errors: list[str]) -> str | None:
    for requirement in requirements:
        if requirement.split("#")[0].strip().startswith("ruff"):
            return _pin(requirement, where, errors)
    errors.append(f"{where}: no ruff requirement found")
    return None


def collect() -> tuple[dict[str, str | None], list[str]]:
    errors: list[str] = []
    data = tomllib.loads(PYPROJECT.read_text())

    found: dict[str, str | None] = {
        "pyproject.toml [build-system] requires": _find(
            data["build-system"]["requires"],
            "pyproject.toml [build-system] requires",
            errors,
        ),
        "pyproject.toml [project.optional-dependencies] dev": _find(
            data["project"]["optional-dependencies"]["dev"],
            "pyproject.toml [project.optional-dependencies] dev",
            errors,
        ),
        "pyproject.toml [tool.hatch.envs.default] dependencies": _find(
            data["tool"]["hatch"]["envs"]["default"]["dependencies"],
            "pyproject.toml [tool.hatch.envs.default] dependencies",
            errors,
        ),
    }

    where = ".pre-commit-config.yaml ruff-pre-commit rev"
    rev = re.search(
        r"repo:\s*https://github\.com/astral-sh/ruff-pre-commit\s*\n(?:\s*#.*\n)*\s*rev:\s*v?(?P<version>\S+)",
        PRE_COMMIT.read_text(),
    )
    if rev is None:
        errors.append(f"{where}: could not be located")
        found[where] = None
    else:
        found[where] = rev.group("version")

    where = ".github/workflows/ci-lint.yml pip install"
    install = re.search(r"pip install (?P<requirement>ruff\S*)", CI_LINT.read_text())
    if install is None:
        errors.append(f"{where}: could not be located")
        found[where] = None
    else:
        found[where] = _pin(install.group("requirement"), where, errors)

    return found, errors


def main() -> int:
    found, errors = collect()

    versions = {version for version in found.values() if version is not None}
    if len(versions) > 1:
        errors.append("the ruff versions declared in this repo disagree")

    if not errors:
        return 0

    print("ruff version pins are out of sync:\n", file=sys.stderr)
    for where, version in found.items():
        print(f"  {version or '(unreadable)':<12}  {where}", file=sys.stderr)
    print("", file=sys.stderr)
    for error in errors:
        print(f"  error: {error}", file=sys.stderr)
    print(
        "\nPin all of them to the same exact version. Two ruff versions format the same"
        "\nfile differently, which is how a generated sync twin becomes permanently"
        "\n'out of date' with nothing in the output naming the cause (#486).",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
