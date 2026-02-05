"""Tests for load test Docker image builder."""

import tarfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from zae_limiter.load.builder import (
    _build_wheel,
    _create_build_context,
    _generate_dockerfile,
    build_and_push_locust_image,
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

    def test_returns_wheel_in_dev_mode(self, tmp_path):
        """Returns wheel path when in development mode."""
        wheel = tmp_path / "dist" / "zae_limiter-0.8.0.whl"
        wheel.parent.mkdir()
        wheel.touch()

        with (
            patch("zae_limiter.load.builder.Path.exists", return_value=True),
            patch("zae_limiter.load.builder._build_wheel", return_value=wheel),
        ):
            result = get_zae_limiter_source()
            assert isinstance(result, Path)


class TestBuildWheel:
    """Tests for _build_wheel."""

    def test_builds_and_returns_wheel(self, tmp_path):
        """_build_wheel runs uv build and returns wheel path."""
        dist_dir = tmp_path / "dist"
        dist_dir.mkdir()
        wheel = dist_dir / "zae_limiter-0.8.0.whl"

        def mock_run(*args, **kwargs):
            wheel.touch()

        with patch("subprocess.run", side_effect=mock_run):
            result = _build_wheel(tmp_path)
            assert result == wheel

    def test_cleans_old_wheels(self, tmp_path):
        """_build_wheel removes old wheels before building."""
        dist_dir = tmp_path / "dist"
        dist_dir.mkdir()
        old_wheel = dist_dir / "zae_limiter-0.7.0.whl"
        old_wheel.touch()
        new_wheel = dist_dir / "zae_limiter-0.8.0.whl"

        def mock_run(*args, **kwargs):
            new_wheel.touch()

        with patch("subprocess.run", side_effect=mock_run):
            _build_wheel(tmp_path)
            assert not old_wheel.exists()

    def test_raises_when_no_wheel_produced(self, tmp_path):
        """_build_wheel raises RuntimeError if no wheel is built."""
        dist_dir = tmp_path / "dist"
        dist_dir.mkdir()

        with (
            patch("subprocess.run"),
            pytest.raises(RuntimeError, match="Wheel build failed"),
        ):
            _build_wheel(tmp_path)


class TestBuildAndPushLocustImage:
    """Tests for build_and_push_locust_image."""

    def test_auto_detects_source_when_none(self, tmp_path):
        """build_and_push_locust_image calls get_zae_limiter_source when source is None."""
        locustfile_dir = tmp_path / "locust"
        locustfile_dir.mkdir()
        (locustfile_dir / "locustfile.py").write_text("# locust")

        mock_docker = MagicMock()
        mock_client = MagicMock()
        mock_docker.from_env.return_value = mock_client
        mock_client.images.build.return_value = (MagicMock(), [])
        mock_client.images.push.return_value = iter([{"status": "ok"}])

        with (
            patch.dict("sys.modules", {"docker": mock_docker}),
            patch("boto3.client") as mock_boto3_client,
            patch(
                "zae_limiter.load.builder.get_zae_limiter_source",
            ) as mock_source,
        ):
            mock_ecr = MagicMock()
            mock_sts = MagicMock()

            def client_factory(service, **kwargs):
                if service == "ecr":
                    return mock_ecr
                elif service == "sts":
                    return mock_sts
                return MagicMock()

            mock_boto3_client.side_effect = client_factory
            mock_sts.get_caller_identity.return_value = {"Account": "123456789"}
            mock_ecr.get_authorization_token.return_value = {
                "authorizationData": [
                    {
                        "authorizationToken": "dXNlcjpwYXNz",
                        "proxyEndpoint": "https://123.dkr.ecr.us-east-1.amazonaws.com",
                    }
                ]
            }
            mock_source.return_value = "0.8.0"

            build_and_push_locust_image("test-load", "us-east-1", locustfile_dir, None)
            mock_source.assert_called_once()

    def test_builds_and_pushes(self, tmp_path):
        """Builds Docker image and pushes to ECR."""
        locustfile_dir = tmp_path / "locust"
        locustfile_dir.mkdir()
        (locustfile_dir / "locustfile.py").write_text("# locust")

        mock_docker = MagicMock()
        mock_client = MagicMock()
        mock_docker.from_env.return_value = mock_client
        mock_client.images.build.return_value = (MagicMock(), [])
        mock_client.images.push.return_value = iter([{"status": "ok"}])

        with (
            patch.dict("sys.modules", {"docker": mock_docker}),
            patch("boto3.client") as mock_boto3_client,
        ):
            mock_ecr = MagicMock()
            mock_sts = MagicMock()

            def client_factory(service, **kwargs):
                if service == "ecr":
                    return mock_ecr
                elif service == "sts":
                    return mock_sts
                return MagicMock()

            mock_boto3_client.side_effect = client_factory
            mock_sts.get_caller_identity.return_value = {"Account": "123456789"}
            mock_ecr.get_authorization_token.return_value = {
                "authorizationData": [
                    {
                        "authorizationToken": "dXNlcjpwYXNz",  # base64("user:pass")
                        "proxyEndpoint": "https://123.dkr.ecr.us-east-1.amazonaws.com",
                    }
                ]
            }

            result = build_and_push_locust_image("test-load", "us-east-1", locustfile_dir, "0.8.0")
            assert "123456789.dkr.ecr.us-east-1.amazonaws.com" in result
            mock_client.images.build.assert_called_once()
            mock_client.images.push.assert_called_once()

    def test_raises_on_push_error(self, tmp_path):
        """Raises RuntimeError when push fails."""
        locustfile_dir = tmp_path / "locust"
        locustfile_dir.mkdir()
        (locustfile_dir / "locustfile.py").write_text("# locust")

        mock_docker = MagicMock()
        mock_client = MagicMock()
        mock_docker.from_env.return_value = mock_client
        mock_client.images.build.return_value = (MagicMock(), [])
        mock_client.images.push.return_value = iter([{"error": "access denied"}])

        with (
            patch.dict("sys.modules", {"docker": mock_docker}),
            patch("boto3.client") as mock_boto3_client,
        ):
            mock_ecr = MagicMock()
            mock_sts = MagicMock()

            def client_factory(service, **kwargs):
                if service == "ecr":
                    return mock_ecr
                elif service == "sts":
                    return mock_sts
                return MagicMock()

            mock_boto3_client.side_effect = client_factory
            mock_sts.get_caller_identity.return_value = {"Account": "123456789"}
            mock_ecr.get_authorization_token.return_value = {
                "authorizationData": [
                    {
                        "authorizationToken": "dXNlcjpwYXNz",
                        "proxyEndpoint": "https://123.dkr.ecr.us-east-1.amazonaws.com",
                    }
                ]
            }

            with pytest.raises(RuntimeError, match="Push failed"):
                build_and_push_locust_image("test-load", "us-east-1", locustfile_dir, "0.8.0")

    def test_raises_when_docker_not_installed(self, tmp_path):
        """Raises RuntimeError when docker package is missing."""
        locustfile_dir = tmp_path / "locust"
        locustfile_dir.mkdir()
        (locustfile_dir / "locustfile.py").write_text("# locust")

        with (
            patch.dict("sys.modules", {"docker": None}),
            pytest.raises(RuntimeError, match="docker package required"),
        ):
            build_and_push_locust_image("test-load", "us-east-1", locustfile_dir, "0.8.0")


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

    def test_dockerfile_default_locustfile(self):
        """Dockerfile CMD uses default locustfile.py path."""
        dockerfile = _generate_dockerfile("0.8.0")

        assert "ENV LOCUSTFILE=locustfile.py" in dockerfile

    def test_dockerfile_with_custom_locustfile(self):
        """Dockerfile sets custom LOCUSTFILE env var."""
        dockerfile = _generate_dockerfile("0.8.0", locustfile="my_locustfiles/api.py")

        assert "ENV LOCUSTFILE=my_locustfiles/api.py" in dockerfile

    def test_dockerfile_includes_rebalancing(self):
        """Dockerfile CMD includes --enable-rebalancing flag."""
        dockerfile = _generate_dockerfile("0.8.0")

        assert "--enable-rebalancing" in dockerfile

    def test_dockerfile_includes_class_picker(self):
        """Dockerfile CMD includes --class-picker flag."""
        dockerfile = _generate_dockerfile("0.8.0")

        assert "--class-picker" in dockerfile

    def test_dockerfile_with_user_requirements(self):
        """Dockerfile includes pip install for user requirements.txt."""
        dockerfile = _generate_dockerfile("0.8.0", has_user_requirements=True)

        assert "COPY userfiles/requirements.txt /tmp/user-requirements.txt" in dockerfile
        assert "pip install -r /tmp/user-requirements.txt" in dockerfile

    def test_dockerfile_without_user_requirements(self):
        """Dockerfile omits user requirements install when not present."""
        dockerfile = _generate_dockerfile("0.8.0", has_user_requirements=False)

        assert "user-requirements.txt" not in dockerfile


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
        """Build context includes all files and subdirectories from locustfile_dir."""
        wheel = tmp_path / "zae_limiter-0.8.0.whl"
        wheel.write_bytes(b"fake wheel")

        locustfile_dir = tmp_path / "locust"
        locustfile_dir.mkdir()
        (locustfile_dir / "locustfile.py").write_text("# locust")
        (locustfile_dir / "config.py").write_text("# config")
        (locustfile_dir / "data.json").write_text("{}")
        (locustfile_dir / "entities.csv").write_text("id,name")
        common = locustfile_dir / "common"
        common.mkdir()
        (common / "__init__.py").write_text("")
        (common / "helpers.py").write_text("# helpers")

        context = _create_build_context(wheel, locustfile_dir)

        with tarfile.open(fileobj=context, mode="r:gz") as tar:
            names = tar.getnames()
            assert "userfiles/locustfile.py" in names
            assert "userfiles/config.py" in names
            assert "userfiles/data.json" in names
            assert "userfiles/entities.csv" in names
            assert "userfiles/common/__init__.py" in names
            assert "userfiles/common/helpers.py" in names

    def test_context_includes_subdirectories(self, tmp_path):
        """Build context includes files and subdirectories."""
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
            assert "userfiles/subdir/nested.py" in names

    def test_context_detects_user_requirements(self, tmp_path):
        """Build context Dockerfile includes user requirements install."""
        wheel = tmp_path / "zae_limiter-0.8.0.whl"
        wheel.write_bytes(b"fake wheel")

        locustfile_dir = tmp_path / "locust"
        locustfile_dir.mkdir()
        (locustfile_dir / "locustfile.py").write_text("# locust")
        (locustfile_dir / "requirements.txt").write_text("pandas>=2.0\n")

        context = _create_build_context(wheel, locustfile_dir)

        with tarfile.open(fileobj=context, mode="r:gz") as tar:
            dockerfile = tar.extractfile("Dockerfile")
            assert dockerfile is not None
            content = dockerfile.read().decode()
            assert "pip install -r /tmp/user-requirements.txt" in content
