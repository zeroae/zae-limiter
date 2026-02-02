"""Tests for stress test Docker image builder."""

import tarfile
from unittest.mock import patch

from zae_limiter.stress.builder import (
    _create_build_context,
    _generate_dockerfile,
    get_zae_limiter_source,
)


class TestGetZaeLimiterSource:
    """Tests for get_zae_limiter_source."""

    def test_returns_version_string_when_installed(self):
        """Returns version string when package is installed (not in dev mode)."""
        with patch("zae_limiter.stress.builder.Path.exists", return_value=False):
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


class TestCreateBuildContext:
    """Tests for _create_build_context."""

    def test_context_contains_dockerfile(self, tmp_path):
        """Build context contains Dockerfile."""
        wheel = tmp_path / "zae_limiter-0.8.0.whl"
        wheel.write_bytes(b"fake wheel")

        # Create locustfile in expected location
        locustfile = tmp_path / "locustfile.py"
        locustfile.write_text("# locust code")

        with patch("zae_limiter.stress.builder._get_locustfile_path", return_value=locustfile):
            context = _create_build_context(wheel)

        # Extract and verify
        with tarfile.open(fileobj=context, mode="r:gz") as tar:
            names = tar.getnames()
            assert "Dockerfile" in names
