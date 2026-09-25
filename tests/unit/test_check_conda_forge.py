"""Tests for the conda-forge availability check (``scripts/check_conda_forge.py``, #604).

Offline: every API call goes through a fake ``fetch`` keyed by URL, so these pin the
decisions the script makes rather than what conda-forge happens to hold today.
"""

from __future__ import annotations

import importlib.util
import sys
import urllib.error
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "check_conda_forge.py"


def _load() -> ModuleType:
    """Import ``scripts/check_conda_forge.py`` (not an installed package) by path."""
    spec = importlib.util.spec_from_file_location("zae_check_conda_forge", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


ccf = _load()


def conda(name: str, *versions: str) -> tuple[str, dict[str, Any]]:
    return ccf.CONDA_API.format(name=name), {
        "versions": list(versions),
        "latest_version": versions[-1],
    }


def pypi(name: str, **releases: bool) -> tuple[str, dict[str, Any]]:
    """PyPI JSON with one file per release; True means that release has an sdist."""
    files = {
        version: [{"packagetype": "sdist" if has_sdist else "bdist_wheel"}]
        for version, has_sdist in releases.items()
    }
    return ccf.PYPI_API.format(name=name), {"releases": files}


def fake_fetch(*responses: tuple[str, Any]):
    table = dict(responses)

    def fetch(url: str) -> Any:
        if url not in table:
            raise ccf.NotFoundError(url)
        value = table[url]
        if isinstance(value, Exception):
            raise value
        return value

    return fetch


class TestNameResolution:
    def test_tzdata_resolves_to_python_tzdata_not_the_iana_package(self):
        """conda-forge ``tzdata`` is the raw IANA database; the map must win over spelling."""
        fetch = fake_fetch(
            conda("tzdata", "2025b", "2026c"),
            conda("python-tzdata", "2026.3", "2026.4"),
            pypi("tzdata", **{"2026.4": True}),
        )
        result = ccf.check_requirement("tzdata", {"tzdata": "python-tzdata"}, fetch)
        assert result.status == ccf.OK
        assert result.conda_name == "python-tzdata"

    def test_a_spelling_match_is_a_warning_not_a_pass(self):
        """Without the map, a same-named package is only a guess and must not read as OK."""
        fetch = fake_fetch(conda("tzdata", "2026.4"), pypi("tzdata", **{"2026.4": True}))
        result = ccf.check_requirement("tzdata", {}, fetch)
        assert result.status == ccf.WARN
        assert "by spelling only" in result.detail

    def test_underscore_spelling_is_found_when_unmapped(self):
        fetch = fake_fetch(
            conda("aws_lambda_builders", "1.67.0"),
            pypi("aws-lambda-builders", **{"1.67.0": True}),
        )
        result = ccf.check_requirement("aws-lambda-builders>=1.40.0", {}, fetch)
        assert result.status == ccf.WARN
        assert result.conda_name == "aws_lambda_builders"

    def test_mapped_name_passes_cleanly(self):
        fetch = fake_fetch(
            conda("aws_lambda_builders", "1.67.0"),
            pypi("aws-lambda-builders", **{"1.67.0": True}),
        )
        mapping = {"aws-lambda-builders": "aws_lambda_builders"}
        result = ccf.check_requirement("aws-lambda-builders>=1.40.0", mapping, fetch)
        assert result.status == ccf.OK

    def test_unmapped_and_unfound_is_verify_by_hand(self):
        result = ccf.check_requirement("mystery-pkg>=1", {}, fake_fetch())
        assert result.status == ccf.WARN
        assert "verify by hand" in result.detail

    def test_mapped_but_missing_from_the_api_fails(self):
        result = ccf.check_requirement("foo>=1", {"foo": "foo"}, fake_fetch())
        assert result.status == ccf.FAIL

    def test_unreachable_api_warns_rather_than_fails(self):
        fetch = fake_fetch((ccf.CONDA_API.format(name="foo"), urllib.error.URLError("down")))
        result = ccf.check_requirement("foo>=1", {"foo": "foo"}, fetch)
        assert result.status == ccf.WARN


class TestConstraint:
    def test_an_older_conda_forge_version_fails(self):
        """The #222 case: conda-forge had cronsim 2.6 when we required >=2.7."""
        fetch = fake_fetch(conda("cronsim", "2.5", "2.6"))
        result = ccf.check_requirement("cronsim>=2.7", {"cronsim": "cronsim"}, fetch)
        assert result.status == ccf.FAIL
        assert "no conda-forge version satisfies" in result.detail

    def test_non_pep440_versions_are_ignored(self):
        """IANA-style versions like 2026c never satisfy a PEP 440 range."""
        fetch = fake_fetch(conda("tzdata", "2025b", "2026c"))
        result = ccf.check_requirement("tzdata>=2020", {}, fetch)
        assert result.status == ccf.FAIL


class TestSdist:
    def test_wheel_only_release_fails(self):
        fetch = fake_fetch(conda("newdep", "1.0"), pypi("newdep", **{"1.0": False}))
        result = ccf.check_requirement("newdep>=1.0", {"newdep": "newdep"}, fetch)
        assert result.status == ccf.FAIL
        assert "no sdist" in result.detail

    def test_accepted_wheel_only_release_warns(self):
        fetch = fake_fetch(conda("cronsim", "2.7"), pypi("cronsim", **{"2.7": False}))
        result = ccf.check_requirement("cronsim>=2.7", {"cronsim": "cronsim"}, fetch)
        assert result.status == ccf.WARN
        assert "accepted" in result.detail

    def test_only_the_newest_release_in_range_is_checked(self):
        """An old wheel-only release outside the range does not matter."""
        fetch = fake_fetch(
            conda("dep", "1.0", "2.0"),
            pypi("dep", **{"1.0": False, "2.0": True}),
        )
        result = ccf.check_requirement("dep>=2.0", {"dep": "dep"}, fetch)
        assert result.status == ccf.OK

    def test_unreadable_pypi_warns(self):
        fetch = fake_fetch(conda("dep", "1.0"))
        result = ccf.check_requirement("dep", {"dep": "dep"}, fetch)
        assert result.status == ccf.WARN
        assert "could not read PyPI" in result.detail


class TestInvertNameMap:
    def test_prefers_the_package_spelled_like_the_pypi_name(self):
        inverted = ccf.invert_name_map({"_dvc": "dvc", "dvc": "dvc"})
        assert inverted["dvc"] == "dvc"

    def test_normalizes_pypi_names(self):
        inverted = ccf.invert_name_map({"python-tzdata": "tzdata", "ruamel.yaml": "ruamel.yaml"})
        assert inverted == {"tzdata": "python-tzdata", "ruamel-yaml": "ruamel.yaml"}

    def test_skips_entries_without_a_pypi_name(self):
        assert ccf.invert_name_map({"libfoo": None}) == {}


@pytest.mark.parametrize(
    ("statuses", "expected_exit"),
    [([ccf.OK, ccf.WARN], 0), ([ccf.OK, ccf.FAIL], 1)],
)
def test_main_exits_nonzero_only_on_failure(monkeypatch, tmp_path, statuses, expected_exit):
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text('[project]\ndependencies = ["a", "b"]\n')
    monkeypatch.setattr(ccf, "PYPROJECT", pyproject)
    monkeypatch.setattr(ccf, "http_fetch", lambda url: {})
    it = iter(statuses)
    monkeypatch.setattr(
        ccf,
        "check_requirement",
        lambda dep, name_map, fetch: ccf.Result(dep, next(it), dep, "detail"),
    )
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))

    assert ccf.main() == expected_exit
    assert "conda-forge availability" in summary.read_text()
