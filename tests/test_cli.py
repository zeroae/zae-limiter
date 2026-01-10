"""Tests for CLI commands."""

from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import pytest
from click.testing import CliRunner

from zae_limiter.cli import cli


@pytest.fixture
def runner() -> CliRunner:
    """Create a Click CLI runner."""
    return CliRunner()


class TestCLI:
    """Test CLI commands."""

    def test_cli_help(self, runner: CliRunner) -> None:
        """Test CLI help message."""
        result = runner.invoke(cli, ["--help"])
        assert result.exit_code == 0
        assert "zae-limiter infrastructure management CLI" in result.output

    def test_deploy_help(self, runner: CliRunner) -> None:
        """Test deploy command help."""
        result = runner.invoke(cli, ["deploy", "--help"])
        assert result.exit_code == 0
        assert "Deploy CloudFormation stack" in result.output
        assert "--table-name" in result.output
        assert "--region" in result.output

    def test_delete_help(self, runner: CliRunner) -> None:
        """Test delete command help."""
        result = runner.invoke(cli, ["delete", "--help"])
        assert result.exit_code == 0
        assert "Delete CloudFormation stack" in result.output
        assert "--stack-name" in result.output

    def test_status_help(self, runner: CliRunner) -> None:
        """Test status command help."""
        result = runner.invoke(cli, ["status", "--help"])
        assert result.exit_code == 0
        assert "Get CloudFormation stack status" in result.output

    def test_cfn_template_help(self, runner: CliRunner) -> None:
        """Test cfn-template command help."""
        result = runner.invoke(cli, ["cfn-template", "--help"])
        assert result.exit_code == 0
        assert "Export CloudFormation template" in result.output

    def test_cfn_template_to_stdout(self, runner: CliRunner) -> None:
        """Test exporting template to stdout."""
        result = runner.invoke(cli, ["cfn-template"])
        assert result.exit_code == 0
        assert "AWSTemplateFormatVersion" in result.output
        assert "AWS::DynamoDB::Table" in result.output
        assert "RateLimitsTable" in result.output

    def test_cfn_template_to_file(self, runner: CliRunner, tmp_path: Path) -> None:
        """Test exporting template to file."""
        output_file = tmp_path / "template.yaml"
        result = runner.invoke(cli, ["cfn-template", "--output", str(output_file)])

        assert result.exit_code == 0
        assert "Template exported to:" in result.output
        assert output_file.exists()

        content = output_file.read_text()
        assert "AWSTemplateFormatVersion" in content
        assert "AWS::DynamoDB::Table" in content

    @patch("zae_limiter.cli.StackManager")
    def test_deploy_default_parameters(self, mock_stack_manager: Mock, runner: CliRunner) -> None:
        """Test deploy command with default parameters."""
        # Mock stack manager
        mock_instance = Mock()
        mock_instance.get_stack_name = Mock(return_value="zae-limiter-rate_limits")
        mock_instance.create_stack = AsyncMock(
            return_value={
                "status": "CREATE_COMPLETE",
                "stack_id": "test-stack-id",
                "stack_name": "zae-limiter-rate_limits",
            }
        )
        mock_instance.deploy_lambda_code = AsyncMock(
            return_value={
                "status": "deployed",
                "function_arn": "arn:aws:lambda:us-east-1:123456789:function:test",
                "code_sha256": "abc123def456",
                "size_bytes": 30000,
            }
        )
        mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
        mock_instance.__aexit__ = AsyncMock(return_value=None)
        mock_stack_manager.return_value = mock_instance

        result = runner.invoke(cli, ["deploy"])

        assert result.exit_code == 0
        assert "Deploying stack: zae-limiter-rate_limits" in result.output
        assert "✓" in result.output

    @patch("zae_limiter.cli.StackManager")
    def test_deploy_custom_parameters(self, mock_stack_manager: Mock, runner: CliRunner) -> None:
        """Test deploy command with custom parameters."""
        mock_instance = Mock()
        mock_instance.get_stack_name = Mock(return_value="my-custom-stack")
        mock_instance.create_stack = AsyncMock(
            return_value={
                "status": "CREATE_COMPLETE",
                "stack_id": "test-stack-id",
            }
        )
        mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
        mock_instance.__aexit__ = AsyncMock(return_value=None)
        mock_stack_manager.return_value = mock_instance

        result = runner.invoke(
            cli,
            [
                "deploy",
                "--table-name",
                "my_table",
                "--stack-name",
                "my-custom-stack",
                "--region",
                "us-west-2",
                "--snapshot-windows",
                "hourly",
                "--retention-days",
                "30",
                "--no-aggregator",
            ],
        )

        assert result.exit_code == 0
        assert "my-custom-stack" in result.output

    @patch("zae_limiter.cli.StackManager")
    def test_deploy_skip_local(self, mock_stack_manager: Mock, runner: CliRunner) -> None:
        """Test deploy skips CloudFormation for local DynamoDB."""
        mock_instance = Mock()
        mock_instance.get_stack_name = Mock(return_value="test-stack")
        mock_instance.create_stack = AsyncMock(
            return_value={
                "status": "skipped_local",
                "stack_id": None,
                "message": "CloudFormation skipped for local DynamoDB",
            }
        )
        mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
        mock_instance.__aexit__ = AsyncMock(return_value=None)
        mock_stack_manager.return_value = mock_instance

        result = runner.invoke(cli, ["deploy"])

        assert result.exit_code == 0
        assert "skipped" in result.output.lower()

    @patch("zae_limiter.cli.StackManager")
    def test_delete_with_confirmation(self, mock_stack_manager: Mock, runner: CliRunner) -> None:
        """Test delete command requires confirmation."""
        mock_instance = Mock()
        mock_stack_manager.return_value = mock_instance

        # No confirmation - should abort
        result = runner.invoke(cli, ["delete", "--stack-name", "test-stack"], input="n\n")
        assert result.exit_code == 1
        assert "Aborted" in result.output

    @patch("zae_limiter.cli.StackManager")
    def test_delete_with_yes_flag(self, mock_stack_manager: Mock, runner: CliRunner) -> None:
        """Test delete command with -y flag."""
        mock_instance = Mock()
        mock_instance.delete_stack = AsyncMock(return_value=None)
        mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
        mock_instance.__aexit__ = AsyncMock(return_value=None)
        mock_stack_manager.return_value = mock_instance

        result = runner.invoke(cli, ["delete", "--stack-name", "test-stack", "--yes", "--wait"])

        assert result.exit_code == 0
        assert "✓" in result.output
        assert "deleted successfully" in result.output

    @patch("zae_limiter.cli.StackManager")
    def test_delete_no_wait(self, mock_stack_manager: Mock, runner: CliRunner) -> None:
        """Test delete command without waiting."""
        mock_instance = Mock()
        mock_instance.delete_stack = AsyncMock(return_value=None)
        mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
        mock_instance.__aexit__ = AsyncMock(return_value=None)
        mock_stack_manager.return_value = mock_instance

        result = runner.invoke(cli, ["delete", "--stack-name", "test-stack", "--yes", "--no-wait"])

        assert result.exit_code == 0
        assert "initiated" in result.output

    @patch("zae_limiter.cli.StackManager")
    def test_status_exists(self, mock_stack_manager: Mock, runner: CliRunner) -> None:
        """Test status command for existing stack."""
        mock_instance = Mock()
        mock_instance.get_stack_status = AsyncMock(return_value="CREATE_COMPLETE")
        mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
        mock_instance.__aexit__ = AsyncMock(return_value=None)
        mock_stack_manager.return_value = mock_instance

        result = runner.invoke(cli, ["status", "--stack-name", "test-stack"])

        assert result.exit_code == 0
        assert "CREATE_COMPLETE" in result.output
        assert "✓ Stack is ready" in result.output

    @patch("zae_limiter.cli.StackManager")
    def test_status_not_found(self, mock_stack_manager: Mock, runner: CliRunner) -> None:
        """Test status command for non-existent stack."""
        mock_instance = Mock()
        mock_instance.get_stack_status = AsyncMock(return_value=None)
        mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
        mock_instance.__aexit__ = AsyncMock(return_value=None)
        mock_stack_manager.return_value = mock_instance

        result = runner.invoke(cli, ["status", "--stack-name", "nonexistent"])

        assert result.exit_code == 1
        assert "not found" in result.output

    @patch("zae_limiter.cli.StackManager")
    def test_status_in_progress(self, mock_stack_manager: Mock, runner: CliRunner) -> None:
        """Test status command for stack in progress."""
        mock_instance = Mock()
        mock_instance.get_stack_status = AsyncMock(return_value="CREATE_IN_PROGRESS")
        mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
        mock_instance.__aexit__ = AsyncMock(return_value=None)
        mock_stack_manager.return_value = mock_instance

        result = runner.invoke(cli, ["status", "--stack-name", "test-stack"])

        assert result.exit_code == 0
        assert "CREATE_IN_PROGRESS" in result.output
        assert "⏳" in result.output

    @patch("zae_limiter.cli.StackManager")
    def test_status_failed(self, mock_stack_manager: Mock, runner: CliRunner) -> None:
        """Test status command for failed stack."""
        mock_instance = Mock()
        mock_instance.get_stack_status = AsyncMock(return_value="CREATE_FAILED")
        mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
        mock_instance.__aexit__ = AsyncMock(return_value=None)
        mock_stack_manager.return_value = mock_instance

        result = runner.invoke(cli, ["status", "--stack-name", "test-stack"])

        assert result.exit_code == 1
        assert "CREATE_FAILED" in result.output
        assert "✗" in result.output
