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
    # Simulate a dependency being installed
    dep_dir = artifacts / "aioboto3"
    dep_dir.mkdir(parents=True, exist_ok=True)
    (dep_dir / "__init__.py").write_text("# mock aioboto3")


def _mock_builder_build_with_placeholder(
    source_dir: str,
    artifacts_dir: str,
    scratch_dir: str,
    manifest_path: str,
    runtime: str,
    architecture: object,
    **kwargs: object,
) -> None:
    """Mock build that also creates placeholder __init__.py and pip-installed zae_limiter."""
    artifacts = Path(artifacts_dir)
    # Simulate placeholder __init__.py copied from source
    (artifacts / "__init__.py").write_text("# placeholder")
    # Simulate pip-installed zae_limiter (should be replaced by local copy)
    pip_pkg = artifacts / "zae_limiter"
    pip_pkg.mkdir(parents=True, exist_ok=True)
    (pip_pkg / "__init__.py").write_text("# pip-installed version")
    # Also install a normal dep
    dep_dir = artifacts / "aioboto3"
    dep_dir.mkdir(parents=True, exist_ok=True)
    (dep_dir / "__init__.py").write_text("# mock aioboto3")


class TestGetRuntimeRequirements:
    """Tests for reading runtime dependencies from metadata."""

    def test_returns_core_deps(self) -> None:
        """Core dependencies are included."""
        from zae_limiter.infra.lambda_builder import _get_runtime_requirements

        reqs = _get_runtime_requirements()
        dep_names = [r.split(">=")[0].split(">")[0].split("==")[0] for r in reqs]
        assert "aioboto3" in dep_names
        assert "boto3" in dep_names

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
        """Test that package contains all required files."""
        from zae_limiter.infra.lambda_builder import build_lambda_package

        with patch("aws_lambda_builders.builder.LambdaBuilder") as mock_builder_cls:
            mock_builder_cls.return_value.build.side_effect = _mock_builder_build
            zip_bytes = build_lambda_package()

        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            files = set(zf.namelist())

            # Must contain aggregator handler
            assert "zae_limiter/aggregator/handler.py" in files
            assert "zae_limiter/aggregator/processor.py" in files

            # Must contain schema (dependency of processor)
            assert "zae_limiter/schema.py" in files

            # Should contain package init
            assert "zae_limiter/__init__.py" in files

            # Should contain mocked dependency
            assert "aioboto3/__init__.py" in files

    def test_placeholder_removed_and_pip_package_replaced(self) -> None:
        """Test that placeholder __init__.py is removed and pip zae_limiter is replaced."""
        from zae_limiter.infra.lambda_builder import build_lambda_package

        with patch("aws_lambda_builders.builder.LambdaBuilder") as mock_builder_cls:
            mock_builder_cls.return_value.build.side_effect = _mock_builder_build_with_placeholder
            zip_bytes = build_lambda_package()

        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            files = set(zf.namelist())

            # Placeholder __init__.py should NOT be at the root
            assert "__init__.py" not in files

            # zae_limiter should contain local copy, not pip-installed version
            assert "zae_limiter/__init__.py" in files
            content = zf.read("zae_limiter/__init__.py").decode()
            assert "pip-installed" not in content

    def test_package_contains_cfn_template(self) -> None:
        """Test that CloudFormation template is included."""
        from zae_limiter.infra.lambda_builder import build_lambda_package

        with patch("aws_lambda_builders.builder.LambdaBuilder") as mock_builder_cls:
            mock_builder_cls.return_value.build.side_effect = _mock_builder_build
            zip_bytes = build_lambda_package()

        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            files = set(zf.namelist())
            assert "zae_limiter/infra/cfn_template.yaml" in files

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

        # Verify requirements.txt contained runtime deps
        assert len(captured_reqs) == 1
        reqs_content = captured_reqs[0]
        assert "aioboto3" in reqs_content
        assert "boto3" in reqs_content

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

        assert info["handler"] == "zae_limiter.aggregator.handler.handler"
        assert isinstance(info["python_files"], int)
        assert info["python_files"] > 5

    def test_runtime_dependencies_included(self) -> None:
        """Test that runtime dependencies are listed in info."""
        from zae_limiter.infra.lambda_builder import get_package_info

        info = get_package_info()
        deps = info["runtime_dependencies"]
        assert isinstance(deps, list)
        assert len(deps) > 0
        dep_text = " ".join(str(d) for d in deps)
        assert "aioboto3" in dep_text


class TestLambdaHandlerUnit:
    """Unit tests for Lambda handler (no Docker)."""

    def test_handler_with_empty_records(self) -> None:
        """Test handler with empty Records list."""
        from zae_limiter.aggregator.handler import handler

        event = {"Records": []}
        result = handler(event, None)

        assert result["statusCode"] == 200
        assert result["body"]["processed"] == 0
        assert result["body"]["snapshots_updated"] == 0

    def test_handler_with_no_records_key(self) -> None:
        """Test handler with missing Records key."""
        from zae_limiter.aggregator.handler import handler

        event = {}
        result = handler(event, None)

        assert result["statusCode"] == 200
        assert result["body"]["processed"] == 0
