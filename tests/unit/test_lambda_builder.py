"""Tests for Lambda package builder."""

import io
import tempfile
import zipfile
from pathlib import Path
from unittest.mock import patch


def _mock_builder_build(
    source_dir: str,
    artifacts_dir: str,
    scratch_dir: str,
    manifest_path: str,
    runtime: str,
    architecture: object,
    **kwargs: object,
) -> None:
    """Mock LambdaBuilder.build() — simulates installing deps into artifacts_dir."""
    artifacts = Path(artifacts_dir)
    # Simulate a dependency being installed (aws-lambda-powertools)
    dep_dir = artifacts / "aws_lambda_powertools"
    dep_dir.mkdir(parents=True, exist_ok=True)
    (dep_dir / "__init__.py").write_text("# mock aws-lambda-powertools")


def _mock_builder_build_with_placeholder(
    source_dir: str,
    artifacts_dir: str,
    scratch_dir: str,
    manifest_path: str,
    runtime: str,
    architecture: object,
    **kwargs: object,
) -> None:
    """Mock build that also creates placeholder __init__.py and pip-installed packages."""
    artifacts = Path(artifacts_dir)
    # Simulate placeholder __init__.py copied from source
    (artifacts / "__init__.py").write_text("# placeholder")
    # Simulate pip-installed zae_limiter (should be replaced by stub)
    pip_pkg = artifacts / "zae_limiter"
    pip_pkg.mkdir(parents=True, exist_ok=True)
    (pip_pkg / "__init__.py").write_text("# pip-installed version")
    # Simulate pip-installed zae_limiter_aggregator (should be replaced by local copy)
    pip_agg = artifacts / "zae_limiter_aggregator"
    pip_agg.mkdir(parents=True, exist_ok=True)
    (pip_agg / "__init__.py").write_text("# pip-installed aggregator")
    # Also install a normal dep
    dep_dir = artifacts / "aws_lambda_powertools"
    dep_dir.mkdir(parents=True, exist_ok=True)
    (dep_dir / "__init__.py").write_text("# mock aws-lambda-powertools")


class TestGetRuntimeRequirements:
    """Tests for reading runtime dependencies from metadata."""

    def test_returns_only_lambda_extra(self) -> None:
        """Only [lambda] extra dependencies are returned."""
        from zae_limiter.infra.lambda_builder import _get_runtime_requirements

        reqs = _get_runtime_requirements()
        dep_names = [r.split(">=")[0].split(">")[0].split("==")[0] for r in reqs]
        assert "aws-lambda-powertools" in dep_names

    def test_returns_empty_when_no_metadata(self) -> None:
        """Returns empty list when package has no requires metadata."""
        from zae_limiter.infra.lambda_builder import _get_runtime_requirements

        with patch(
            "zae_limiter.infra.lambda_builder.importlib.metadata.requires", return_value=None
        ):
            reqs = _get_runtime_requirements()
        assert reqs == []

    def test_excludes_dev_extras(self) -> None:
        """Dev/docs/cdk extras are excluded."""
        from zae_limiter.infra.lambda_builder import _get_runtime_requirements

        reqs = _get_runtime_requirements()
        dep_text = " ".join(reqs)
        assert "pytest" not in dep_text
        assert "mkdocs" not in dep_text
        assert "aws-cdk-lib" not in dep_text

    def test_excludes_core_deps(self) -> None:
        """Core dependencies are not included (only [lambda] extra)."""
        from zae_limiter.infra.lambda_builder import _get_runtime_requirements

        reqs = _get_runtime_requirements()
        dep_names = [r.split(">=")[0].split(">")[0].split("==")[0] for r in reqs]
        for pkg in ("aiobotocore", "boto3", "click", "pip", "python-ulid"):
            assert pkg not in dep_names, f"{pkg} should not be in [lambda] extra deps"

    def test_includes_lambda_extra(self) -> None:
        """Lambda extra dependencies are included."""
        from zae_limiter.infra.lambda_builder import _get_runtime_requirements

        reqs = _get_runtime_requirements()
        dep_names = [r.split(">=")[0].split(">")[0].split("==")[0] for r in reqs]
        assert "aws-lambda-powertools" in dep_names


class TestBuildLambdaPackage:
    """Tests for building Lambda deployment packages."""

    def test_build_lambda_package(self) -> None:
        """Test that Lambda package builds successfully with mocked builder."""
        from zae_limiter.infra.lambda_builder import build_lambda_package

        with patch("aws_lambda_builders.builder.LambdaBuilder") as mock_builder_cls:
            mock_builder_cls.return_value.build.side_effect = _mock_builder_build
            zip_bytes = build_lambda_package()

        assert isinstance(zip_bytes, bytes)
        assert len(zip_bytes) > 0

        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            assert zf.testzip() is None

    def test_package_contains_required_files(self) -> None:
        """Test that package contains aggregator and schema stub."""
        from zae_limiter.infra.lambda_builder import build_lambda_package

        with patch("aws_lambda_builders.builder.LambdaBuilder") as mock_builder_cls:
            mock_builder_cls.return_value.build.side_effect = _mock_builder_build
            zip_bytes = build_lambda_package()

        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            files = set(zf.namelist())

            # Must contain aggregator handler (new top-level package)
            assert "zae_limiter_aggregator/handler.py" in files
            assert "zae_limiter_aggregator/processor.py" in files
            assert "zae_limiter_aggregator/__init__.py" in files

            # Must contain schema stub
            assert "zae_limiter/schema.py" in files
            assert "zae_limiter/__init__.py" in files

            # Should contain mocked dependency
            assert "aws_lambda_powertools/__init__.py" in files

    def test_zae_limiter_init_is_empty_stub(self) -> None:
        """Test that zae_limiter/__init__.py is an empty stub."""
        from zae_limiter.infra.lambda_builder import build_lambda_package

        with patch("aws_lambda_builders.builder.LambdaBuilder") as mock_builder_cls:
            mock_builder_cls.return_value.build.side_effect = _mock_builder_build
            zip_bytes = build_lambda_package()

        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            content = zf.read("zae_limiter/__init__.py").decode()
            assert content == ""

    def test_package_does_not_contain_full_zae_limiter(self) -> None:
        """Test that the full zae_limiter package is not included."""
        from zae_limiter.infra.lambda_builder import build_lambda_package

        with patch("aws_lambda_builders.builder.LambdaBuilder") as mock_builder_cls:
            mock_builder_cls.return_value.build.side_effect = _mock_builder_build
            zip_bytes = build_lambda_package()

        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            files = set(zf.namelist())

            # Should NOT contain full zae_limiter package files
            assert "zae_limiter/limiter.py" not in files
            assert "zae_limiter/repository.py" not in files
            assert "zae_limiter/cli.py" not in files
            assert "zae_limiter/infra/cfn_template.yaml" not in files

    def test_placeholder_removed_and_pip_packages_replaced(self) -> None:
        """Test that placeholder __init__.py is removed and pip packages are replaced."""
        from zae_limiter.infra.lambda_builder import build_lambda_package

        with patch("aws_lambda_builders.builder.LambdaBuilder") as mock_builder_cls:
            mock_builder_cls.return_value.build.side_effect = _mock_builder_build_with_placeholder
            zip_bytes = build_lambda_package()

        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            files = set(zf.namelist())

            # Placeholder __init__.py should NOT be at the root
            assert "__init__.py" not in files

            # zae_limiter/__init__.py should be the empty stub, not pip-installed version
            assert "zae_limiter/__init__.py" in files
            content = zf.read("zae_limiter/__init__.py").decode()
            assert "pip-installed" not in content
            assert content == ""

            # zae_limiter_aggregator should be local copy, not pip-installed
            assert "zae_limiter_aggregator/__init__.py" in files
            content = zf.read("zae_limiter_aggregator/__init__.py").decode()
            assert "pip-installed" not in content

    def test_builder_called_with_correct_args(self) -> None:
        """Test that LambdaBuilder is invoked with correct parameters."""
        from zae_limiter.infra.lambda_builder import build_lambda_package

        captured_reqs: list[str] = []

        def _capturing_build(
            source_dir: str,
            artifacts_dir: str,
            scratch_dir: str,
            manifest_path: str,
            runtime: str,
            architecture: object,
            **kwargs: object,
        ) -> None:
            # Capture requirements.txt content before temp dir is cleaned up
            captured_reqs.append(Path(manifest_path).read_text())
            _mock_builder_build(
                source_dir,
                artifacts_dir,
                scratch_dir,
                manifest_path,
                runtime,
                architecture,
                **kwargs,
            )

        with patch("aws_lambda_builders.builder.LambdaBuilder") as mock_builder_cls:
            mock_builder_cls.return_value.build.side_effect = _capturing_build
            build_lambda_package()

        # Verify constructor
        mock_builder_cls.assert_called_once_with(
            language="python",
            dependency_manager="pip",
            application_framework=None,
        )

        # Verify requirements.txt contains only [lambda] extra deps
        assert len(captured_reqs) == 1
        reqs_content = captured_reqs[0]
        assert "aws-lambda-powertools" in reqs_content
        # Core deps should NOT be in requirements.txt
        reqs_lines = reqs_content.strip().splitlines()
        assert not any(line.startswith("aiobotocore") for line in reqs_lines)
        assert not any(line.startswith("boto3") for line in reqs_lines)
        assert not any(line.startswith("click") for line in reqs_lines)

    def test_write_lambda_package(self) -> None:
        """Test writing package to file."""
        from zae_limiter.infra.lambda_builder import write_lambda_package

        with (
            tempfile.TemporaryDirectory() as tmpdir,
            patch("aws_lambda_builders.builder.LambdaBuilder") as mock_builder_cls,
        ):
            mock_builder_cls.return_value.build.side_effect = _mock_builder_build
            output_path = Path(tmpdir) / "lambda.zip"
            size = write_lambda_package(output_path)

            assert output_path.exists()
            assert output_path.stat().st_size == size

            with zipfile.ZipFile(output_path) as zf:
                assert zf.testzip() is None


class TestGetPackageInfo:
    """Tests for package info retrieval."""

    def test_get_package_info(self) -> None:
        """Test getting package metadata."""
        from zae_limiter.infra.lambda_builder import get_package_info

        info = get_package_info()

        assert "package_path" in info
        assert "python_files" in info
        assert "uncompressed_size" in info
        assert "handler" in info
        assert "runtime_dependencies" in info

        assert info["handler"] == "zae_limiter_aggregator.handler.handler"
        assert isinstance(info["python_files"], int)
        assert info["python_files"] > 0

    def test_runtime_dependencies_included(self) -> None:
        """Test that runtime dependencies are listed in info."""
        from zae_limiter.infra.lambda_builder import get_package_info

        info = get_package_info()
        deps = info["runtime_dependencies"]
        assert isinstance(deps, list)
        assert len(deps) > 0
        dep_text = " ".join(str(d) for d in deps)
        assert "aws-lambda-powertools" in dep_text


class TestLambdaHandlerUnit:
    """Unit tests for Lambda handler (no Docker)."""

    def test_handler_with_empty_records(self) -> None:
        """Test handler with empty Records list."""
        from zae_limiter_aggregator.handler import handler

        event = {"Records": []}
        result = handler(event, None)

        assert result["statusCode"] == 200
        assert result["body"]["processed"] == 0
        assert result["body"]["snapshots_updated"] == 0

    def test_handler_with_no_records_key(self) -> None:
        """Test handler with missing Records key."""
        from zae_limiter_aggregator.handler import handler

        event = {}
        result = handler(event, None)

        assert result["statusCode"] == 200
        assert result["body"]["processed"] == 0


class TestVendoredSchedule:
    """`schedule.py` must be packaged, or the aggregator dies at cold start (#222)."""

    def _build(self) -> bytes:
        from zae_limiter.infra.lambda_builder import build_lambda_package

        with patch("aws_lambda_builders.builder.LambdaBuilder") as mock_builder_cls:
            mock_builder_cls.return_value.build.side_effect = _mock_builder_build
            return build_lambda_package()

    def test_aggregator_package_vendors_schedule(self) -> None:
        with zipfile.ZipFile(io.BytesIO(self._build())) as zf:
            assert "zae_limiter/schedule.py" in zf.namelist()

    def test_every_import_in_the_package_resolves(self) -> None:
        """The general form of the test above, and the one that keeps working.

        Nothing in the unit suite notices a missing vendored module: every test
        imports the installed package, where it is present. This reads the zip.
        """
        from tests.fixtures.lambda_packages import assert_package_imports_resolve
        from zae_limiter.infra.lambda_builder import _get_runtime_requirements

        assert_package_imports_resolve(self._build(), _get_runtime_requirements())

    def test_lambda_extra_carries_cronsim_and_tzdata(self) -> None:
        """`schedule.py` needs cronsim, and zoneinfo needs tzdata because the
        Lambda image may ship no /usr/share/zoneinfo."""
        from zae_limiter.infra.lambda_builder import _get_runtime_requirements

        reqs = _get_runtime_requirements()
        assert any(r.startswith("cronsim") for r in reqs), reqs
        assert any(r.startswith("tzdata") for r in reqs), reqs

    def test_croniter_never_reaches_the_lambda(self) -> None:
        """croniter is the dev-only schedule oracle and must stay dev-only."""
        from zae_limiter.infra.lambda_builder import _get_runtime_requirements

        assert not any("croniter" in r for r in _get_runtime_requirements())


def _modes(zip_bytes: bytes) -> dict[str, int]:
    """Map each archive entry to the permission bits Lambda will extract it with."""
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        return {i.filename: (i.external_attr >> 16) & 0o777 for i in zf.infolist()}


class TestZipPermissions:
    """Lambda runs the handler as a non-root user, so every entry must be world-readable.

    ``ZipFile.write`` copied each file's on-disk mode into the archive, so a host
    with ``umask 077`` built a zip of ``0600`` files and the function failed at
    cold start with ``PermissionError: [Errno 13]``.
    """

    def test_restrictive_source_modes_are_normalised(self, tmp_path: Path) -> None:
        from zae_limiter.infra.lambda_builder import write_lambda_zip

        (tmp_path / "pkg").mkdir()
        private = tmp_path / "pkg" / "__init__.py"
        private.write_text("x = 1\n")
        private.chmod(0o600)
        script = tmp_path / "tool"
        script.write_text("#!/bin/sh\n")
        script.chmod(0o700)

        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
            write_lambda_zip(zf, tmp_path)

        assert _modes(buffer.getvalue()) == {"pkg/__init__.py": 0o644, "tool": 0o755}
        with zipfile.ZipFile(buffer) as zf:
            assert zf.read("pkg/__init__.py") == b"x = 1\n"
            assert zf.getinfo("pkg/__init__.py").compress_type == zipfile.ZIP_DEFLATED

    def test_package_built_under_umask_077_is_world_readable(self, restrictive_umask: None) -> None:
        from zae_limiter.infra.lambda_builder import build_lambda_package

        with patch("aws_lambda_builders.builder.LambdaBuilder") as mock_builder_cls:
            mock_builder_cls.return_value.build.side_effect = _mock_builder_build
            modes = _modes(build_lambda_package())

        assert "zae_limiter/__init__.py" in modes
        assert {name for name, mode in modes.items() if mode & 0o444 != 0o444} == set()
