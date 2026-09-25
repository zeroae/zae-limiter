#!/usr/bin/env python3
"""Check that every runtime dependency can be satisfied on conda-forge.

zae-limiter ships on conda-forge as well as PyPI, so each entry in ``[project]
dependencies`` needs a conda-forge package whose versions satisfy the declared range,
and a PyPI release with an sdist for the feedstock to build from. When #222 added
``cronsim>=2.7`` nobody checked; conda-forge had 2.6 and 2.7 shipped wheel-only, and
the v0.14.0 conda-forge build was blocked after the release had gone out everywhere
else (#604). See ``.claude/rules/dependencies.md``.

For each dependency:

1. **Resolve the conda-forge name** through prefix.dev's parselmouth conda-forge ->
   PyPI map, inverted. Spelling is not enough: PyPI ``tzdata`` is conda-forge
   ``python-tzdata``, and conda-forge ``tzdata`` is a *different* package (the raw
   IANA database), so a lookup by spelling passes against the wrong thing.
2. **Check the range** against the versions on ``api.anaconda.org`` (never the web
   page, which answers 200 for packages that do not exist). Pre-releases never count:
   conda does not install them by default, and ``packaging`` would otherwise let one
   satisfy a plain ``>=`` range - it reads the IANA-style ``2026c`` as ``2026rc0``.
3. **Check for an sdist** on the newest PyPI release inside the range, which is what
   the conda-forge autotick bot would pick up.

A problem that blocks a conda-forge build fails the run (exit 1). Anything the script
cannot decide - a name the map does not know, an API it cannot reach - is a warning
to verify by hand, never a failure, so a network blip cannot block a pull request.

Run directly, or via the ``conda-forge-check`` workflow.
"""

from __future__ import annotations

import json
import os
import re
import sys
import tomllib
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from packaging.requirements import Requirement
from packaging.version import InvalidVersion, Version

REPO_ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = REPO_ROOT / "pyproject.toml"

NAME_MAP_URL = (
    "https://raw.githubusercontent.com/prefix-dev/parselmouth/main/files/mapping_as_grayskull.json"
)
CONDA_API = "https://api.anaconda.org/package/conda-forge/{name}"
PYPI_API = "https://pypi.org/pypi/{name}/json"

# Wheel-only dependencies whose feedstock already builds from another source. Each entry
# needs the reason and where that was settled; a new wheel-only dependency is a failure
# until someone has checked its feedstock and added it here.
NO_SDIST_OK: dict[str, str] = {
    "cronsim": "2.7 is wheel-only; the feedstock builds from the GitHub tag archive "
    "(conda-forge/cronsim-feedstock#3)",
}

OK, WARN, FAIL = "ok", "warn", "fail"

Fetch = Callable[[str], Any]


class NotFoundError(Exception):
    """The API answered 404."""


@dataclass
class Result:
    requirement: str
    status: str
    conda_name: str | None
    detail: str


def normalize(name: str) -> str:
    """PEP 503 name normalization."""
    return re.sub(r"[-_.]+", "-", name).lower()


def invert_name_map(conda_to_pypi: dict[str, str | None]) -> dict[str, str]:
    """Turn parselmouth's conda-forge -> PyPI map into PyPI -> conda-forge.

    Several conda-forge packages can claim one PyPI name (``dvc`` and ``_dvc`` both map
    to ``dvc``); prefer the one spelled like the PyPI name, then one without a leading
    underscore, then the alphabetically first, so the choice is deterministic.
    """
    candidates: dict[str, list[str]] = {}
    for conda_name, pypi_name in conda_to_pypi.items():
        if pypi_name:
            candidates.setdefault(normalize(pypi_name), []).append(conda_name)
    inverted = {}
    for pypi_name, names in candidates.items():
        inverted[pypi_name] = min(
            names,
            key=lambda n: (normalize(n) != pypi_name, n.startswith("_"), n),
        )
    return inverted


def _versions(raw: list[str]) -> list[Version]:
    parsed = []
    for value in raw:
        try:
            parsed.append(Version(value))
        except InvalidVersion:
            continue
    return parsed


def _lookup(fetch: Fetch, name: str) -> dict[str, Any] | None:
    try:
        package: dict[str, Any] = fetch(CONDA_API.format(name=name))
    except NotFoundError:
        return None
    return package


def check_requirement(requirement: str, name_map: dict[str, str] | None, fetch: Fetch) -> Result:
    """Check one ``[project] dependencies`` entry against conda-forge and PyPI."""
    req = Requirement(requirement)
    pypi_name = normalize(req.name)
    warnings: list[str] = []

    # 1. Resolve the conda-forge name.
    try:
        if name_map is not None and pypi_name in name_map:
            conda_name = name_map[pypi_name]
            package = _lookup(fetch, conda_name)
            if package is None:
                return Result(
                    requirement,
                    FAIL,
                    conda_name,
                    f"mapped to conda-forge `{conda_name}`, but the API has no such package",
                )
        else:
            spellings = dict.fromkeys([req.name.lower(), pypi_name, pypi_name.replace("-", "_")])
            conda_name, package = None, None
            for spelling in spellings:
                package = _lookup(fetch, spelling)
                if package is not None:
                    conda_name = spelling
                    break
            if package is None:
                return Result(
                    requirement,
                    WARN,
                    None,
                    "not in the name map and no conda-forge package by that spelling; "
                    "verify by hand",
                )
            warnings.append(
                f"not in the name map; matched `{conda_name}` by spelling only, verify it is "
                "the same package"
            )
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return Result(requirement, WARN, None, f"could not reach api.anaconda.org ({exc})")

    # 2. Check the declared range against what conda-forge has.
    satisfying = [
        v
        for v in _versions(package.get("versions", []))
        if req.specifier.contains(v, prereleases=False)
    ]
    if not satisfying:
        return Result(
            requirement,
            FAIL,
            conda_name,
            f"no conda-forge version satisfies `{req.specifier}` "
            f"(latest there is {package.get('latest_version')})",
        )
    best = max(satisfying)

    # 3. The newest PyPI release in range must publish an sdist.
    try:
        pypi = fetch(PYPI_API.format(name=req.name))
    except (NotFoundError, urllib.error.URLError, TimeoutError, OSError) as exc:
        warnings.append(f"could not read PyPI to check for an sdist ({exc})")
    else:
        releases = {
            v: files
            for v, files in ((_parse(k), f) for k, f in pypi.get("releases", {}).items())
            if v is not None and files and req.specifier.contains(v, prereleases=False)
        }
        if releases:
            newest = max(releases)
            if not any(f.get("packagetype") == "sdist" for f in releases[newest]):
                if pypi_name in NO_SDIST_OK:
                    warnings.append(
                        f"PyPI {newest} has no sdist (accepted: {NO_SDIST_OK[pypi_name]})"
                    )
                else:
                    return Result(
                        requirement,
                        FAIL,
                        conda_name,
                        f"PyPI {newest} publishes no sdist, which breaks the conda-forge "
                        "recipe and the autotick bot; see .claude/rules/dependencies.md",
                    )

    detail = f"conda-forge `{conda_name}` {best} satisfies `{req.specifier or 'any'}`"
    return Result(requirement, WARN if warnings else OK, conda_name, "; ".join([detail, *warnings]))


def _parse(value: str) -> Version | None:
    try:
        return Version(value)
    except InvalidVersion:
        return None


def http_fetch(url: str) -> Any:
    request = urllib.request.Request(url, headers={"User-Agent": "zae-limiter-conda-check"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise NotFoundError(url) from exc
        raise


def report(results: list[Result]) -> str:
    icons = {OK: "✅", WARN: "⚠️", FAIL: "❌"}
    lines = [
        "## conda-forge availability",
        "",
        "| | Dependency | conda-forge | Detail |",
        "|-|------------|-------------|--------|",
    ]
    for r in results:
        lines.append(
            f"| {icons[r.status]} | `{r.requirement}` | {r.conda_name or '—'} | {r.detail} |"
        )
    return "\n".join(lines) + "\n"


def main() -> int:
    dependencies = tomllib.loads(PYPROJECT.read_text())["project"]["dependencies"]
    try:
        name_map: dict[str, str] | None = invert_name_map(http_fetch(NAME_MAP_URL))
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        print(f"warning: could not load the parselmouth name map ({exc}); matching by spelling")
        name_map = None

    results = [check_requirement(dep, name_map, http_fetch) for dep in dependencies]
    text = report(results)
    print(text)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a") as fh:
            fh.write(text)

    failures = [r for r in results if r.status == FAIL]
    if failures:
        print(
            f"{len(failures)} dependency(ies) cannot be satisfied on conda-forge; "
            "see .claude/rules/dependencies.md"
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
