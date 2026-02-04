"""Tests for load test Lambda package builder."""

import zipfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from zae_limiter.load.lambda_builder import _generate_requirements


class TestGenerateRequirements:
    """Tests for _generate_requirements."""

    def test_with_version_string(self):
        result = _generate_requirements("0.8.0")
        assert "zae-limiter==0.8.0" in result

    def test_with_wheel_path(self, tmp_path):
        wheel = tmp_path / "zae_limiter-0.8.0.whl"
        result = _generate_requirements(wheel)
        assert str(wheel) in result


class TestBuildLoadLambdaPackage:
    """Tests for build_load_lambda_package."""

    def test_creates_zip_file(self, tmp_path):
        """build_load_lambda_package creates a zip file."""
        from zae_limiter.load.lambda_builder import build_load_lambda_package

        locustfile_dir = tmp_path / "locust"
        locustfile_dir.mkdir()
        (locustfile_dir / "locustfile.py").write_text("# locust code")

        with patch("aws_lambda_builders.builder.LambdaBuilder") as mock_builder_class:
            mock_builder = MagicMock()
            mock_builder_class.return_value = mock_builder

            def mock_build(**kwargs):
                artifacts_dir = Path(kwargs["artifacts_dir"])
                (artifacts_dir / "test_module.py").write_text("# test")

            mock_builder.build.side_effect = mock_build

            output_dir = tmp_path / "output"
            zip_path = build_load_lambda_package(
                zae_limiter_source="0.8.0",
                locustfile_dir=locustfile_dir,
                output_dir=output_dir,
            )

            assert zip_path.exists()
            assert zip_path.suffix == ".zip"

    def test_zip_includes_all_files_from_dir(self, tmp_path):
        """Built zip includes all files from locustfile_dir."""
        from zae_limiter.load.lambda_builder import build_load_lambda_package

        locustfile_dir = tmp_path / "locust"
        locustfile_dir.mkdir()
        (locustfile_dir / "locustfile.py").write_text("# locust code")
        (locustfile_dir / "config.py").write_text("# config")
        (locustfile_dir / "data.json").write_text("{}")

        with patch("aws_lambda_builders.builder.LambdaBuilder") as mock_builder_class:
            mock_builder = MagicMock()
            mock_builder_class.return_value = mock_builder

            def mock_build(**kwargs):
                artifacts_dir = Path(kwargs["artifacts_dir"])
                (artifacts_dir / "locust").mkdir()
                (artifacts_dir / "locust" / "__init__.py").write_text("")

            mock_builder.build.side_effect = mock_build

            output_dir = tmp_path / "output"
            zip_path = build_load_lambda_package(
                zae_limiter_source="0.8.0",
                locustfile_dir=locustfile_dir,
                output_dir=output_dir,
            )

            with zipfile.ZipFile(zip_path, "r") as zf:
                names = zf.namelist()
                assert "locustfile.py" in names
                assert "config.py" in names
                assert "data.json" in names

    def test_default_output_dir(self, tmp_path, monkeypatch):
        """Uses build/ as default output_dir when not provided."""
        from zae_limiter.load.lambda_builder import build_load_lambda_package

        locustfile_dir = tmp_path / "locust"
        locustfile_dir.mkdir()
        (locustfile_dir / "locustfile.py").write_text("# locust code")

        # Change cwd so build/ is created in tmp_path
        monkeypatch.chdir(tmp_path)

        with patch("aws_lambda_builders.builder.LambdaBuilder") as mock_builder_class:
            mock_builder = MagicMock()
            mock_builder_class.return_value = mock_builder

            def mock_build(**kwargs):
                artifacts_dir = Path(kwargs["artifacts_dir"])
                (artifacts_dir / "module.py").write_text("# test")

            mock_builder.build.side_effect = mock_build

            # Call without output_dir to hit the default branch
            zip_path = build_load_lambda_package(
                zae_limiter_source="0.8.0",
                locustfile_dir=locustfile_dir,
                output_dir=None,
            )

            assert zip_path.parent.name == "build"
            assert zip_path.exists()

    def test_removes_placeholder_init(self, tmp_path):
        """Removes __init__.py placeholder from artifacts."""
        from zae_limiter.load.lambda_builder import build_load_lambda_package

        locustfile_dir = tmp_path / "locust"
        locustfile_dir.mkdir()
        (locustfile_dir / "locustfile.py").write_text("# locust code")

        with patch("aws_lambda_builders.builder.LambdaBuilder") as mock_builder_class:
            mock_builder = MagicMock()
            mock_builder_class.return_value = mock_builder

            def mock_build(**kwargs):
                artifacts_dir = Path(kwargs["artifacts_dir"])
                # Simulate aws-lambda-builders leaving __init__.py
                (artifacts_dir / "__init__.py").write_text("")
                (artifacts_dir / "locust_pkg").mkdir()
                (artifacts_dir / "locust_pkg" / "__init__.py").write_text("")

            mock_builder.build.side_effect = mock_build

            output_dir = tmp_path / "output"
            zip_path = build_load_lambda_package(
                zae_limiter_source="0.8.0",
                locustfile_dir=locustfile_dir,
                output_dir=output_dir,
            )

            with zipfile.ZipFile(zip_path, "r") as zf:
                names = zf.namelist()
                # Root __init__.py should have been removed
                assert "__init__.py" not in names
