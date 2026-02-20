"""Tests for provisioner Lambda package builder."""

import io
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


class TestProvisionerBuilder:
    """Tests for build_provisioner_package."""

    def test_package_contains_provisioner_modules(self):
        """Built package contains all zae_limiter_provisioner .py files."""
        from zae_limiter.infra.provisioner_builder import build_provisioner_package

        with patch("aws_lambda_builders.builder.LambdaBuilder") as mock_builder_cls:
            mock_builder_cls.return_value.build.side_effect = _mock_builder_build
            zip_bytes = build_provisioner_package()

        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            names = zf.namelist()
            assert "zae_limiter_provisioner/__init__.py" in names
            assert "zae_limiter_provisioner/handler.py" in names
            assert "zae_limiter_provisioner/manifest.py" in names
            assert "zae_limiter_provisioner/differ.py" in names
            assert "zae_limiter_provisioner/applier.py" in names

    def test_package_contains_zae_limiter_stubs(self):
        """Built package contains minimal zae_limiter stubs."""
        from zae_limiter.infra.provisioner_builder import build_provisioner_package

        with patch("aws_lambda_builders.builder.LambdaBuilder") as mock_builder_cls:
            mock_builder_cls.return_value.build.side_effect = _mock_builder_build
            zip_bytes = build_provisioner_package()

        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            names = zf.namelist()
            assert "zae_limiter/__init__.py" in names
            assert "zae_limiter/schema.py" in names
            assert "zae_limiter/models.py" in names
            assert "zae_limiter/exceptions.py" in names

    def test_zae_limiter_init_is_empty_stub(self):
        """zae_limiter/__init__.py is an empty stub, not the full package."""
        from zae_limiter.infra.provisioner_builder import build_provisioner_package

        with patch("aws_lambda_builders.builder.LambdaBuilder") as mock_builder_cls:
            mock_builder_cls.return_value.build.side_effect = _mock_builder_build
            zip_bytes = build_provisioner_package()

        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            content = zf.read("zae_limiter/__init__.py").decode()
            assert content == ""

    def test_package_does_not_contain_full_zae_limiter(self):
        """Full zae_limiter package files are excluded."""
        from zae_limiter.infra.provisioner_builder import build_provisioner_package

        with patch("aws_lambda_builders.builder.LambdaBuilder") as mock_builder_cls:
            mock_builder_cls.return_value.build.side_effect = _mock_builder_build
            zip_bytes = build_provisioner_package()

        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            names = zf.namelist()
            assert "zae_limiter/limiter.py" not in names
            assert "zae_limiter/repository.py" not in names
            assert "zae_limiter/cli.py" not in names

    def test_requirements_include_pyyaml(self):
        """Requirements.txt includes pyyaml for YAML parsing."""
        from zae_limiter.infra.provisioner_builder import build_provisioner_package

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
            build_provisioner_package()

        assert len(captured_reqs) == 1
        assert "pyyaml" in captured_reqs[0].lower()

    def test_get_provisioner_handler_path(self):
        """Handler path matches CFN Handler property."""
        from zae_limiter.infra.provisioner_builder import get_provisioner_info

        info = get_provisioner_info()
        assert info["handler"] == "zae_limiter_provisioner.handler.on_event"

    def test_get_provisioner_info_metadata(self):
        """Provisioner info contains expected metadata fields."""
        from zae_limiter.infra.provisioner_builder import get_provisioner_info

        info = get_provisioner_info()
        assert "package_path" in info
        assert "python_files" in info
        assert "uncompressed_size" in info
        assert "runtime_dependencies" in info
        assert isinstance(info["python_files"], int)
        assert info["python_files"] > 0

    def test_get_runtime_requirements_returns_pyyaml_when_no_metadata(self):
        """Returns pyyaml fallback when package metadata has no requires."""
        from zae_limiter.infra.provisioner_builder import _get_runtime_requirements

        with patch(
            "zae_limiter.infra.provisioner_builder.importlib.metadata.requires",
            return_value=None,
        ):
            result = _get_runtime_requirements()

        assert result == ["pyyaml>=6.0"]

    def test_placeholder_removed_from_artifacts(self):
        """Placeholder __init__.py is removed from artifacts after build."""
        from zae_limiter.infra.provisioner_builder import build_provisioner_package

        def _build_with_placeholder(
            source_dir: str,
            artifacts_dir: str,
            scratch_dir: str,
            manifest_path: str,
            runtime: str,
            architecture: object,
            **kwargs: object,
        ) -> None:
            _mock_builder_build(
                source_dir, artifacts_dir, scratch_dir, manifest_path, runtime, architecture
            )
            # Simulate aws-lambda-builders copying __init__.py to artifacts
            (Path(artifacts_dir) / "__init__.py").write_text("placeholder")

        with patch("aws_lambda_builders.builder.LambdaBuilder") as mock_builder_cls:
            mock_builder_cls.return_value.build.side_effect = _build_with_placeholder
            zip_bytes = build_provisioner_package()

        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            # Top-level __init__.py should NOT be in the zip
            assert "__init__.py" not in zf.namelist()
