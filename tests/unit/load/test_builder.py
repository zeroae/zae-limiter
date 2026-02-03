"""Tests for load test Docker image builder."""

import tarfile
from unittest.mock import patch

from zae_limiter.load.builder import (
    _create_build_context,
    _generate_dockerfile,
    get_zae_limiter_source,
)


class TestGetZaeLimiterSource:
    """Tests for get_zae_limiter_source."""

    def test_returns_version_string_when_installed(self):
        """Returns version string when package is installed (not in dev mode)."""
        with patch("zae_limiter.load.builder.Path.exists", return_value=False):
            with patch("importlib.metadata.version", return_value="0.8.0"):
                result = get_zae_limiter_source()
                assert result == "0.8.0"


class TestGenerateDockerfile:
    """Tests for _generate_dockerfile."""

    def test_dockerfile_with_wheel(self, tmp_path):
        """Dockerfile installs from wheel when wheel path provided."""
        wheel = tmp_path / "zae_limiter-0.8.0.whl"
        wheel.touch()

        dockerfile = _generate_dockerfile(wheel)

        assert "COPY wheels/*.whl /tmp/" in dockerfile
        assert "pip install /tmp/*.whl" in dockerfile

    def test_dockerfile_with_version(self):
        """Dockerfile installs from PyPI when version string provided."""
        dockerfile = _generate_dockerfile("0.8.0")

        assert "pip install zae-limiter==0.8.0" in dockerfile

    def test_dockerfile_copies_userfiles(self):
        """Dockerfile copies userfiles directory."""
        dockerfile = _generate_dockerfile("0.8.0")

        assert "COPY userfiles/ /mnt/" in dockerfile


class TestCreateBuildContext:
    """Tests for _create_build_context."""

    def test_context_contains_dockerfile(self, tmp_path):
        """Build context contains Dockerfile."""
        wheel = tmp_path / "zae_limiter-0.8.0.whl"
        wheel.write_bytes(b"fake wheel")

        locustfile_dir = tmp_path / "locust"
        locustfile_dir.mkdir()
        (locustfile_dir / "locustfile.py").write_text("# locust code")

        context = _create_build_context(wheel, locustfile_dir)

        with tarfile.open(fileobj=context, mode="r:gz") as tar:
            names = tar.getnames()
            assert "Dockerfile" in names

    def test_context_includes_all_files_from_dir(self, tmp_path):
        """Build context includes all files from locustfile_dir."""
        wheel = tmp_path / "zae_limiter-0.8.0.whl"
        wheel.write_bytes(b"fake wheel")

        locustfile_dir = tmp_path / "locust"
        locustfile_dir.mkdir()
        (locustfile_dir / "locustfile.py").write_text("# locust")
        (locustfile_dir / "config.py").write_text("# config")
        (locustfile_dir / "data.json").write_text("{}")
        (locustfile_dir / "entities.csv").write_text("id,name")

        context = _create_build_context(wheel, locustfile_dir)

        with tarfile.open(fileobj=context, mode="r:gz") as tar:
            names = tar.getnames()
            assert "userfiles/locustfile.py" in names
            assert "userfiles/config.py" in names
            assert "userfiles/data.json" in names
            assert "userfiles/entities.csv" in names

    def test_context_excludes_subdirectories(self, tmp_path):
        """Build context only includes files, not subdirectories."""
        wheel = tmp_path / "zae_limiter-0.8.0.whl"
        wheel.write_bytes(b"fake wheel")

        locustfile_dir = tmp_path / "locust"
        locustfile_dir.mkdir()
        (locustfile_dir / "locustfile.py").write_text("# locust")
        subdir = locustfile_dir / "subdir"
        subdir.mkdir()
        (subdir / "nested.py").write_text("# nested")

        context = _create_build_context(wheel, locustfile_dir)

        with tarfile.open(fileobj=context, mode="r:gz") as tar:
            names = tar.getnames()
            assert "userfiles/locustfile.py" in names
            assert not any("nested" in n for n in names)
