"""Tests for stress test Lambda package builder."""

import zipfile
from pathlib import Path
from unittest.mock import MagicMock, patch


class TestBuildStressLambdaPackage:
    """Tests for build_stress_lambda_package."""

    def test_creates_zip_file(self, tmp_path):
        """build_stress_lambda_package creates a zip file."""
        from zae_limiter.stress.lambda_builder import build_stress_lambda_package

        # Mock the LambdaBuilder to avoid actual pip installs
        with patch("aws_lambda_builders.builder.LambdaBuilder") as mock_builder_class:
            mock_builder = MagicMock()
            mock_builder_class.return_value = mock_builder

            # Mock the build to just create an empty artifacts dir
            def mock_build(**kwargs):
                artifacts_dir = Path(kwargs["artifacts_dir"])
                (artifacts_dir / "test_module.py").write_text("# test")

            mock_builder.build.side_effect = mock_build

            zip_path = build_stress_lambda_package(
                zae_limiter_source="0.8.0",
                output_dir=tmp_path,
            )

            assert zip_path.exists()
            assert zip_path.suffix == ".zip"

    def test_zip_contains_stress_lambda(self, tmp_path):
        """Built zip contains stress_lambda package."""
        from zae_limiter.stress.lambda_builder import build_stress_lambda_package

        with patch("aws_lambda_builders.builder.LambdaBuilder") as mock_builder_class:
            mock_builder = MagicMock()
            mock_builder_class.return_value = mock_builder

            def mock_build(**kwargs):
                artifacts_dir = Path(kwargs["artifacts_dir"])
                (artifacts_dir / "locust").mkdir()
                (artifacts_dir / "locust" / "__init__.py").write_text("")

            mock_builder.build.side_effect = mock_build

            zip_path = build_stress_lambda_package(
                zae_limiter_source="0.8.0",
                output_dir=tmp_path,
            )

            with zipfile.ZipFile(zip_path, "r") as zf:
                names = zf.namelist()
                # Should contain config.py (copied from stress module)
                assert any("config.py" in n for n in names)
