"""Tests for local development commands."""

from __future__ import annotations

from unittest.mock import MagicMock, Mock, patch

import pytest
from click.testing import CliRunner

from zae_limiter.cli import cli
from zae_limiter.local import (
    CONTAINER_NAME,
    DEFAULT_IMAGE,
    DEFAULT_PORT,
    LOCALSTACK_SERVICES,
)


@pytest.fixture
def runner() -> CliRunner:
    """Create a Click CLI runner."""
    return CliRunner()


class _MockDockerErrors:
    """Namespace holding mock exception types for the docker.errors module."""

    NotFound = type("NotFound", (Exception,), {})
    DockerException = type("DockerException", (Exception,), {})
    APIError = type("APIError", (Exception,), {})
    ImageNotFound = type("ImageNotFound", (Exception,), {})


@pytest.fixture
def mock_docker():
    """Mock the docker module used by local commands.

    This must be used by any test that executes a command function body
    (not just help), since the commands reference the module-level ``docker``.
    """
    with patch("zae_limiter.local.docker") as mock_mod:
        mock_mod.errors = _MockDockerErrors
        yield mock_mod


class TestLocalHelp:
    """Test local command help messages."""

    def test_local_help(self, runner: CliRunner) -> None:
        """Test local group help."""
        result = runner.invoke(cli, ["local", "--help"])
        assert result.exit_code == 0
        assert "Local development" in result.output
        assert "env" in result.output
        assert "up" in result.output
        assert "down" in result.output
        assert "status" in result.output
        assert "logs" in result.output

    def test_local_env_help(self, runner: CliRunner) -> None:
        """Test local env help."""
        result = runner.invoke(cli, ["local", "env", "--help"])
        assert result.exit_code == 0
        assert "--format" in result.output
        assert "--port" in result.output
        assert "eval" in result.output
        assert "direnv" in result.output
        assert "powershell" in result.output

    def test_local_up_help(self, runner: CliRunner) -> None:
        """Test local up help."""
        result = runner.invoke(cli, ["local", "up", "--help"])
        assert result.exit_code == 0
        assert "--docker-host" in result.output
        assert "--image" in result.output
        assert "--port" in result.output
        assert "--name" in result.output

    def test_local_down_help(self, runner: CliRunner) -> None:
        """Test local down help."""
        result = runner.invoke(cli, ["local", "down", "--help"])
        assert result.exit_code == 0
        assert "--docker-host" in result.output

    def test_local_status_help(self, runner: CliRunner) -> None:
        """Test local status help."""
        result = runner.invoke(cli, ["local", "status", "--help"])
        assert result.exit_code == 0
        assert "--docker-host" in result.output

    def test_local_logs_help(self, runner: CliRunner) -> None:
        """Test local logs help."""
        result = runner.invoke(cli, ["local", "logs", "--help"])
        assert result.exit_code == 0
        assert "--follow" in result.output
        assert "--tail" in result.output


class TestLocalEnv:
    """Test local env command."""

    def test_env_default_format(self, runner: CliRunner) -> None:
        """Test default (eval) format outputs export statements."""
        result = runner.invoke(cli, ["local", "env"])

        assert result.exit_code == 0
        assert "export AWS_ENDPOINT_URL=http://localhost:4566" in result.output
        assert "export AWS_ACCESS_KEY_ID=test" in result.output
        assert "export AWS_SECRET_ACCESS_KEY=test" in result.output
        assert "export AWS_DEFAULT_REGION=us-east-1" in result.output

    def test_env_eval_format(self, runner: CliRunner) -> None:
        """Test explicit eval format."""
        result = runner.invoke(cli, ["local", "env", "--format", "eval"])

        assert result.exit_code == 0
        assert "export AWS_ENDPOINT_URL=http://localhost:4566" in result.output

    def test_env_direnv_format(self, runner: CliRunner) -> None:
        """Test direnv format outputs KEY=VALUE without export prefix."""
        result = runner.invoke(cli, ["local", "env", "--format", "direnv"])

        assert result.exit_code == 0
        assert "AWS_ENDPOINT_URL=http://localhost:4566" in result.output
        assert "AWS_ACCESS_KEY_ID=test" in result.output
        assert "AWS_SECRET_ACCESS_KEY=test" in result.output
        assert "AWS_DEFAULT_REGION=us-east-1" in result.output
        assert "export" not in result.output

    def test_env_powershell_format(self, runner: CliRunner) -> None:
        """Test powershell format outputs $env:KEY = "VALUE" lines."""
        result = runner.invoke(cli, ["local", "env", "--format", "powershell"])

        assert result.exit_code == 0
        assert '$env:AWS_ENDPOINT_URL = "http://localhost:4566"' in result.output
        assert '$env:AWS_ACCESS_KEY_ID = "test"' in result.output
        assert '$env:AWS_SECRET_ACCESS_KEY = "test"' in result.output
        assert '$env:AWS_DEFAULT_REGION = "us-east-1"' in result.output

    def test_env_custom_port(self, runner: CliRunner) -> None:
        """Test --port changes the endpoint URL."""
        result = runner.invoke(cli, ["local", "env", "--port", "4510"])

        assert result.exit_code == 0
        assert "http://localhost:4510" in result.output
        assert "http://localhost:4566" not in result.output

    def test_env_custom_port_with_direnv(self, runner: CliRunner) -> None:
        """Test --port works with direnv format."""
        result = runner.invoke(cli, ["local", "env", "--format", "direnv", "--port", "5000"])

        assert result.exit_code == 0
        assert "AWS_ENDPOINT_URL=http://localhost:5000" in result.output
        assert "export" not in result.output

    def test_env_invalid_format(self, runner: CliRunner) -> None:
        """Test invalid format is rejected by click.Choice."""
        result = runner.invoke(cli, ["local", "env", "--format", "fish"])

        assert result.exit_code != 0

    def test_env_no_docker_required(self, runner: CliRunner) -> None:
        """Test env command works without docker installed."""
        from unittest.mock import patch

        with patch("zae_limiter.local.docker", None):
            result = runner.invoke(cli, ["local", "env"])

        assert result.exit_code == 0
        assert "export AWS_ENDPOINT_URL" in result.output


class TestGetDockerClient:
    """Test _get_docker_client helper."""

    def test_docker_not_installed(self, runner: CliRunner) -> None:
        """Test error when docker package is not installed."""
        with patch("zae_limiter.local.docker", None):
            from zae_limiter.local import _get_docker_client

            with pytest.raises(SystemExit):
                _get_docker_client()

    def test_docker_host_override(self, mock_docker: Mock) -> None:
        """Test connecting with a custom docker host."""
        from zae_limiter.local import _get_docker_client

        _get_docker_client(docker_host="tcp://remote:2375")
        mock_docker.DockerClient.assert_called_once_with(base_url="tcp://remote:2375")

    def test_docker_from_env(self, mock_docker: Mock) -> None:
        """Test connecting with default docker env."""
        from zae_limiter.local import _get_docker_client

        _get_docker_client()
        mock_docker.from_env.assert_called_once()

    def test_docker_connection_error(self, mock_docker: Mock) -> None:
        """Test error when Docker daemon is not reachable."""
        from zae_limiter.local import _get_docker_client

        mock_docker.from_env.side_effect = mock_docker.errors.DockerException("refused")

        with pytest.raises(SystemExit):
            _get_docker_client()


class TestFindContainer:
    """Test _find_container helper."""

    def test_find_existing(self, mock_docker: Mock) -> None:
        """Test finding an existing container."""
        from zae_limiter.local import _find_container

        mock_client = MagicMock()
        result = _find_container(mock_client)
        assert result is not None
        mock_client.containers.get.assert_called_once_with(CONTAINER_NAME)

    def test_find_not_found(self, mock_docker: Mock) -> None:
        """Test returning None when container doesn't exist."""
        from zae_limiter.local import _find_container

        mock_client = MagicMock()
        mock_client.containers.get.side_effect = mock_docker.errors.NotFound("nope")

        result = _find_container(mock_client)
        assert result is None


class TestLocalUp:
    """Test local up command."""

    @patch("zae_limiter.local._wait_for_health", return_value=True)
    @patch("zae_limiter.local._ensure_image")
    @patch("zae_limiter.local._find_container", return_value=None)
    @patch("zae_limiter.local._get_docker_client")
    def test_up_starts_new_container(
        self,
        mock_client_fn: Mock,
        _mock_find: Mock,
        _mock_image: Mock,
        _mock_health: Mock,
        mock_docker: Mock,
        runner: CliRunner,
    ) -> None:
        """Test starting a new container when none exists."""
        mock_client = MagicMock()
        mock_client.containers.run.return_value = MagicMock()
        mock_client_fn.return_value = mock_client

        result = runner.invoke(cli, ["local", "up"])

        assert result.exit_code == 0
        assert "LocalStack is ready" in result.output
        assert 'eval "$(zae-limiter local env)"' in result.output
        mock_client.containers.run.assert_called_once()

    @patch("zae_limiter.local._find_container")
    @patch("zae_limiter.local._get_docker_client")
    def test_up_already_running(
        self,
        mock_client_fn: Mock,
        mock_find: Mock,
        runner: CliRunner,
    ) -> None:
        """Test when container is already running."""
        mock_container = MagicMock()
        mock_container.status = "running"
        mock_find.return_value = mock_container
        mock_client_fn.return_value = MagicMock()

        result = runner.invoke(cli, ["local", "up"])

        assert result.exit_code == 0
        assert "already running" in result.output

    @patch("zae_limiter.local._wait_for_health", return_value=True)
    @patch("zae_limiter.local._ensure_image")
    @patch("zae_limiter.local._find_container")
    @patch("zae_limiter.local._get_docker_client")
    def test_up_removes_stopped_container(
        self,
        mock_client_fn: Mock,
        mock_find: Mock,
        _mock_image: Mock,
        _mock_health: Mock,
        mock_docker: Mock,
        runner: CliRunner,
    ) -> None:
        """Test that stopped containers are removed before creating a new one."""
        mock_stopped = MagicMock()
        mock_stopped.status = "exited"
        mock_find.return_value = mock_stopped

        mock_client = MagicMock()
        mock_client.containers.run.return_value = MagicMock()
        mock_client_fn.return_value = mock_client

        result = runner.invoke(cli, ["local", "up"])

        assert result.exit_code == 0
        mock_stopped.remove.assert_called_once()

    @patch("zae_limiter.local._wait_for_health", return_value=True)
    @patch("zae_limiter.local._ensure_image")
    @patch("zae_limiter.local._find_container", return_value=None)
    @patch("zae_limiter.local._get_docker_client")
    def test_up_with_name_shows_deploy_instructions(
        self,
        mock_client_fn: Mock,
        _mock_find: Mock,
        _mock_image: Mock,
        _mock_health: Mock,
        mock_docker: Mock,
        runner: CliRunner,
    ) -> None:
        """Test that --name shows deploy instructions."""
        mock_client = MagicMock()
        mock_client.containers.run.return_value = MagicMock()
        mock_client_fn.return_value = mock_client

        result = runner.invoke(cli, ["local", "up", "--name", "my-app"])

        assert result.exit_code == 0
        assert "zae-limiter deploy --name my-app" in result.output
        assert "--endpoint-url" in result.output

    @patch("zae_limiter.local._wait_for_health", return_value=False)
    @patch("zae_limiter.local._ensure_image")
    @patch("zae_limiter.local._find_container", return_value=None)
    @patch("zae_limiter.local._get_docker_client")
    def test_up_health_timeout(
        self,
        mock_client_fn: Mock,
        _mock_find: Mock,
        _mock_image: Mock,
        _mock_health: Mock,
        mock_docker: Mock,
        runner: CliRunner,
    ) -> None:
        """Test exit code when health check times out."""
        mock_client = MagicMock()
        mock_client.containers.run.return_value = MagicMock()
        mock_client_fn.return_value = mock_client

        result = runner.invoke(cli, ["local", "up"])

        assert result.exit_code != 0
        assert "did not become healthy" in result.output

    @patch("zae_limiter.local._ensure_image")
    @patch("zae_limiter.local._find_container", return_value=None)
    @patch("zae_limiter.local._get_docker_client")
    def test_up_port_conflict(
        self,
        mock_client_fn: Mock,
        _mock_find: Mock,
        _mock_image: Mock,
        mock_docker: Mock,
        runner: CliRunner,
    ) -> None:
        """Test helpful error message on port conflict."""
        mock_client = MagicMock()
        port_error = mock_docker.errors.APIError("port is already allocated")
        mock_client.containers.run.side_effect = port_error
        mock_client_fn.return_value = mock_client

        result = runner.invoke(cli, ["local", "up"])

        assert result.exit_code != 0
        assert "already in use" in result.output.lower()

    @patch("zae_limiter.local._ensure_image")
    @patch("zae_limiter.local._find_container", return_value=None)
    @patch("zae_limiter.local._get_docker_client")
    def test_up_generic_api_error(
        self,
        mock_client_fn: Mock,
        _mock_find: Mock,
        _mock_image: Mock,
        mock_docker: Mock,
        runner: CliRunner,
    ) -> None:
        """Test generic API error during container start."""
        mock_client = MagicMock()
        mock_client.containers.run.side_effect = mock_docker.errors.APIError("some other error")
        mock_client_fn.return_value = mock_client

        result = runner.invoke(cli, ["local", "up"])

        assert result.exit_code != 0
        assert "failed to start container" in result.output.lower()


class TestLocalDown:
    """Test local down command."""

    @patch("zae_limiter.local._find_container")
    @patch("zae_limiter.local._get_docker_client")
    def test_down_stops_container(
        self,
        mock_client_fn: Mock,
        mock_find: Mock,
        mock_docker: Mock,
        runner: CliRunner,
    ) -> None:
        """Test stopping a running container."""
        mock_container = MagicMock()
        mock_find.return_value = mock_container
        mock_client_fn.return_value = MagicMock()

        result = runner.invoke(cli, ["local", "down"])

        assert result.exit_code == 0
        assert "stopped" in result.output.lower()
        mock_container.stop.assert_called_once_with(timeout=10)
        mock_container.remove.assert_called_once()

    @patch("zae_limiter.local._find_container", return_value=None)
    @patch("zae_limiter.local._get_docker_client")
    def test_down_not_running(
        self,
        mock_client_fn: Mock,
        _mock_find: Mock,
        mock_docker: Mock,
        runner: CliRunner,
    ) -> None:
        """Test down when container is not running."""
        mock_client_fn.return_value = MagicMock()

        result = runner.invoke(cli, ["local", "down"])

        assert result.exit_code == 0
        assert "not running" in result.output.lower()

    @patch("zae_limiter.local._find_container")
    @patch("zae_limiter.local._get_docker_client")
    def test_down_handles_api_error(
        self,
        mock_client_fn: Mock,
        mock_find: Mock,
        mock_docker: Mock,
        runner: CliRunner,
    ) -> None:
        """Test down handles Docker API errors gracefully."""
        mock_container = MagicMock()
        mock_container.stop.side_effect = mock_docker.errors.APIError("daemon error")
        mock_find.return_value = mock_container
        mock_client_fn.return_value = MagicMock()

        result = runner.invoke(cli, ["local", "down"])

        assert result.exit_code != 0
        assert "failed to stop" in result.output.lower()

    @patch("zae_limiter.local._find_container")
    @patch("zae_limiter.local._get_docker_client")
    def test_down_handles_already_removed(
        self,
        mock_client_fn: Mock,
        mock_find: Mock,
        mock_docker: Mock,
        runner: CliRunner,
    ) -> None:
        """Test down handles container already removed during stop."""
        mock_container = MagicMock()
        mock_container.stop.side_effect = mock_docker.errors.NotFound("already gone")
        mock_find.return_value = mock_container
        mock_client_fn.return_value = MagicMock()

        result = runner.invoke(cli, ["local", "down"])

        assert result.exit_code == 0
        assert "stopped" in result.output.lower()


class TestLocalStatus:
    """Test local status command."""

    @patch("zae_limiter.local._find_container")
    @patch("zae_limiter.local._get_docker_client")
    def test_status_running(
        self,
        mock_client_fn: Mock,
        mock_find: Mock,
        mock_docker: Mock,
        runner: CliRunner,
    ) -> None:
        """Test status when container is running."""
        mock_container = MagicMock()
        mock_container.status = "running"
        mock_container.attrs = {
            "State": {"Health": {"Status": "healthy"}},
            "NetworkSettings": {"Ports": {"4566/tcp": [{"HostIp": "0.0.0.0", "HostPort": "4566"}]}},
            "Config": {"Image": DEFAULT_IMAGE},
        }
        mock_find.return_value = mock_container
        mock_client_fn.return_value = MagicMock()

        result = runner.invoke(cli, ["local", "status"])

        assert result.exit_code == 0
        assert "running" in result.output.lower()
        assert "healthy" in result.output.lower()
        assert "http://localhost:4566" in result.output
        assert LOCALSTACK_SERVICES in result.output
        assert 'eval "$(zae-limiter local env)"' in result.output
        assert "direnv" in result.output
        assert "powershell" in result.output.lower()

    @patch("zae_limiter.local._find_container")
    @patch("zae_limiter.local._get_docker_client")
    def test_status_unhealthy_omits_env_vars(
        self,
        mock_client_fn: Mock,
        mock_find: Mock,
        mock_docker: Mock,
        runner: CliRunner,
    ) -> None:
        """Test status omits env var instructions when container is unhealthy."""
        mock_container = MagicMock()
        mock_container.status = "running"
        mock_container.attrs = {
            "State": {"Health": {"Status": "unhealthy"}},
            "NetworkSettings": {"Ports": {"4566/tcp": [{"HostIp": "0.0.0.0", "HostPort": "4566"}]}},
            "Config": {"Image": DEFAULT_IMAGE},
        }
        mock_find.return_value = mock_container
        mock_client_fn.return_value = MagicMock()

        result = runner.invoke(cli, ["local", "status"])

        assert result.exit_code == 0
        assert "unhealthy" in result.output.lower()
        assert "zae-limiter local env" not in result.output

    @patch("zae_limiter.local._find_container", return_value=None)
    @patch("zae_limiter.local._get_docker_client")
    def test_status_not_running(
        self,
        mock_client_fn: Mock,
        _mock_find: Mock,
        mock_docker: Mock,
        runner: CliRunner,
    ) -> None:
        """Test status when container is not found."""
        mock_client_fn.return_value = MagicMock()

        result = runner.invoke(cli, ["local", "status"])

        assert result.exit_code == 0
        assert "not running" in result.output.lower()

    @patch("zae_limiter.local._find_container")
    @patch("zae_limiter.local._get_docker_client")
    def test_status_container_removed_during_reload(
        self,
        mock_client_fn: Mock,
        mock_find: Mock,
        mock_docker: Mock,
        runner: CliRunner,
    ) -> None:
        """Test status handles container removed during reload."""
        mock_container = MagicMock()
        mock_container.reload.side_effect = mock_docker.errors.NotFound("gone")
        mock_find.return_value = mock_container
        mock_client_fn.return_value = MagicMock()

        result = runner.invoke(cli, ["local", "status"])

        assert result.exit_code == 0
        assert "not running" in result.output.lower()


class TestLocalLogs:
    """Test local logs command."""

    @patch("zae_limiter.local._find_container")
    @patch("zae_limiter.local._get_docker_client")
    def test_logs_default(
        self,
        mock_client_fn: Mock,
        mock_find: Mock,
        mock_docker: Mock,
        runner: CliRunner,
    ) -> None:
        """Test logs with default options."""
        mock_container = MagicMock()
        mock_container.logs.return_value = b"line 1\nline 2\n"
        mock_find.return_value = mock_container
        mock_client_fn.return_value = MagicMock()

        result = runner.invoke(cli, ["local", "logs"])

        assert result.exit_code == 0
        assert "line 1" in result.output
        assert "line 2" in result.output
        mock_container.logs.assert_called_once_with(tail=100)

    @patch("zae_limiter.local._find_container")
    @patch("zae_limiter.local._get_docker_client")
    def test_logs_with_tail(
        self,
        mock_client_fn: Mock,
        mock_find: Mock,
        mock_docker: Mock,
        runner: CliRunner,
    ) -> None:
        """Test logs with custom tail."""
        mock_container = MagicMock()
        mock_container.logs.return_value = b"recent line\n"
        mock_find.return_value = mock_container
        mock_client_fn.return_value = MagicMock()

        result = runner.invoke(cli, ["local", "logs", "--tail", "50"])

        assert result.exit_code == 0
        mock_container.logs.assert_called_once_with(tail=50)

    @patch("zae_limiter.local._find_container")
    @patch("zae_limiter.local._get_docker_client")
    def test_logs_with_follow(
        self,
        mock_client_fn: Mock,
        mock_find: Mock,
        mock_docker: Mock,
        runner: CliRunner,
    ) -> None:
        """Test logs with follow mode."""
        mock_container = MagicMock()
        mock_container.logs.return_value = iter([b"line 1\n", b"line 2\n"])
        mock_find.return_value = mock_container
        mock_client_fn.return_value = MagicMock()

        result = runner.invoke(cli, ["local", "logs", "--follow"])

        assert result.exit_code == 0
        assert "line 1" in result.output
        mock_container.logs.assert_called_once_with(stream=True, follow=True, tail=100)

    @patch("zae_limiter.local._find_container", return_value=None)
    @patch("zae_limiter.local._get_docker_client")
    def test_logs_not_running(
        self,
        mock_client_fn: Mock,
        _mock_find: Mock,
        mock_docker: Mock,
        runner: CliRunner,
    ) -> None:
        """Test logs when container is not running."""
        mock_client_fn.return_value = MagicMock()

        result = runner.invoke(cli, ["local", "logs"])

        assert result.exit_code != 0
        assert "not running" in result.output.lower()

    @patch("zae_limiter.local._find_container")
    @patch("zae_limiter.local._get_docker_client")
    def test_logs_container_stops_during_follow(
        self,
        mock_client_fn: Mock,
        mock_find: Mock,
        mock_docker: Mock,
        runner: CliRunner,
    ) -> None:
        """Test logs handles container stopping during follow."""
        mock_container = MagicMock()
        mock_container.logs.side_effect = mock_docker.errors.NotFound("gone")
        mock_find.return_value = mock_container
        mock_client_fn.return_value = MagicMock()

        result = runner.invoke(cli, ["local", "logs", "--follow"])

        assert "container stopped" in result.output.lower()

    @patch("zae_limiter.local._find_container")
    @patch("zae_limiter.local._get_docker_client")
    def test_logs_api_error(
        self,
        mock_client_fn: Mock,
        mock_find: Mock,
        mock_docker: Mock,
        runner: CliRunner,
    ) -> None:
        """Test logs handles API error during streaming."""
        mock_container = MagicMock()
        mock_container.logs.side_effect = mock_docker.errors.APIError("connection lost")
        mock_find.return_value = mock_container
        mock_client_fn.return_value = MagicMock()

        result = runner.invoke(cli, ["local", "logs"])

        assert result.exit_code != 0
        assert "lost connection" in result.output.lower()

    @patch("zae_limiter.local._find_container")
    @patch("zae_limiter.local._get_docker_client")
    def test_logs_keyboard_interrupt(
        self,
        mock_client_fn: Mock,
        mock_find: Mock,
        mock_docker: Mock,
        runner: CliRunner,
    ) -> None:
        """Test logs handles KeyboardInterrupt gracefully."""
        mock_container = MagicMock()
        mock_container.logs.side_effect = KeyboardInterrupt()
        mock_find.return_value = mock_container
        mock_client_fn.return_value = MagicMock()

        result = runner.invoke(cli, ["local", "logs"])

        assert result.exit_code == 0


class TestWaitForHealth:
    """Test health check behavior."""

    def test_health_detects_exited_container(self) -> None:
        """Test that health check fails fast when container exits."""
        from zae_limiter.local import _wait_for_health

        mock_container = MagicMock()
        mock_container.status = "exited"
        mock_container.attrs = {"State": {}}
        mock_container.logs.return_value = b"some error\n"

        result = _wait_for_health(mock_container)

        assert result is False
        # Should call reload only once (fail fast, no loop)
        mock_container.reload.assert_called_once()

    def test_health_detects_dead_container(self) -> None:
        """Test that health check fails fast when container is dead."""
        from zae_limiter.local import _wait_for_health

        mock_container = MagicMock()
        mock_container.status = "dead"
        mock_container.attrs = {"State": {}}
        mock_container.logs.return_value = b""

        result = _wait_for_health(mock_container)

        assert result is False
        mock_container.reload.assert_called_once()

    def test_health_detects_unhealthy(self) -> None:
        """Test that health check returns False for unhealthy container."""
        from zae_limiter.local import _wait_for_health

        mock_container = MagicMock()
        mock_container.status = "running"
        mock_container.attrs = {"State": {"Health": {"Status": "unhealthy"}}}

        result = _wait_for_health(mock_container)

        assert result is False

    @patch("zae_limiter.local.HEALTH_CHECK_RETRIES", 2)
    @patch("zae_limiter.local.HEALTH_CHECK_INTERVAL", 0)
    def test_health_timeout(self) -> None:
        """Test that health check returns False after retries exhausted."""
        from zae_limiter.local import _wait_for_health

        mock_container = MagicMock()
        mock_container.status = "running"
        mock_container.attrs = {"State": {"Health": {"Status": "starting"}}}

        result = _wait_for_health(mock_container)

        assert result is False
        assert mock_container.reload.call_count == 2

    def test_health_crash_log_exception(self) -> None:
        """Test health check handles log read failure during crash."""
        from zae_limiter.local import _wait_for_health

        mock_container = MagicMock()
        mock_container.status = "exited"
        mock_container.attrs = {"State": {}}
        mock_container.logs.side_effect = Exception("cannot read logs")

        result = _wait_for_health(mock_container)

        assert result is False


class TestEnsureImage:
    """Test image pull behavior."""

    def test_ensure_image_already_present(self, mock_docker: Mock) -> None:
        """Test no pull when image exists."""
        from zae_limiter.local import _ensure_image

        mock_client = MagicMock()
        _ensure_image(mock_client, DEFAULT_IMAGE)

        mock_client.images.get.assert_called_once_with(DEFAULT_IMAGE)
        mock_client.images.pull.assert_not_called()

    def test_ensure_image_pulls_missing(self, mock_docker: Mock) -> None:
        """Test image is pulled when not present."""
        from zae_limiter.local import _ensure_image

        mock_client = MagicMock()
        mock_client.images.get.side_effect = mock_docker.errors.ImageNotFound("not found")

        _ensure_image(mock_client, DEFAULT_IMAGE)

        mock_client.images.pull.assert_called_once_with(DEFAULT_IMAGE)

    def test_ensure_image_pull_fails(self, mock_docker: Mock) -> None:
        """Test exit on pull failure."""
        from zae_limiter.local import _ensure_image

        mock_client = MagicMock()
        mock_client.images.get.side_effect = mock_docker.errors.ImageNotFound("not found")
        mock_client.images.pull.side_effect = mock_docker.errors.APIError("network error")

        with pytest.raises(SystemExit):
            _ensure_image(mock_client, DEFAULT_IMAGE)


class TestConstants:
    """Test that module constants match expected values."""

    def test_container_name(self) -> None:
        assert CONTAINER_NAME == "zae-limiter-localstack"

    def test_default_image(self) -> None:
        assert DEFAULT_IMAGE == "localstack/localstack:4.14"

    def test_default_port(self) -> None:
        assert DEFAULT_PORT == 4566

    def test_services_includes_all_required(self) -> None:
        """Verify all required services are in the services list."""
        required = [
            "dynamodb",
            "dynamodbstreams",
            "lambda",
            "cloudformation",
            "logs",
            "iam",
            "cloudwatch",
            "sqs",
            "s3",
            "sts",
            "resourcegroupstaggingapi",
        ]
        for service in required:
            assert service in LOCALSTACK_SERVICES, f"Missing service: {service}"
