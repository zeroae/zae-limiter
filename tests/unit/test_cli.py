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
        assert "--name" in result.output
        assert "--region" in result.output
        assert "--endpoint-url" in result.output
        # New options
        assert "--lambda-timeout" in result.output
        assert "--lambda-memory" in result.output
        assert "--enable-alarms" in result.output
        assert "--no-alarms" in result.output
        assert "--alarm-sns-topic" in result.output
        assert "--lambda-duration-threshold-pct" in result.output

    def test_delete_help(self, runner: CliRunner) -> None:
        """Test delete command help."""
        result = runner.invoke(cli, ["delete", "--help"])
        assert result.exit_code == 0
        assert "Delete CloudFormation stack" in result.output
        assert "--name" in result.output

    def test_status_help(self, runner: CliRunner) -> None:
        """Test status command help."""
        result = runner.invoke(cli, ["status", "--help"])
        assert result.exit_code == 0
        assert "Get comprehensive status" in result.output

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
        mock_instance.stack_name = "ZAEL-rate-limits"
        mock_instance.table_name = "ZAEL-rate-limits"
        mock_instance.create_stack = AsyncMock(
            return_value={
                "status": "CREATE_COMPLETE",
                "stack_id": "test-stack-id",
                "stack_name": "ZAEL-rate-limits",
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
        assert "Deploying stack: ZAEL-rate-limits" in result.output
        assert "✓" in result.output

        # Verify default values for new parameters via StackOptions
        call_args = mock_instance.create_stack.call_args
        assert call_args is not None
        stack_options = call_args[1]["stack_options"]
        # Default values: lambda_timeout=60, lambda_memory=256, alarms enabled, 80% threshold
        assert stack_options.lambda_timeout == 60
        assert stack_options.lambda_memory == 256
        assert stack_options.enable_alarms is True
        assert stack_options.lambda_duration_threshold_pct == 80

    @patch("zae_limiter.cli.StackManager")
    def test_deploy_custom_parameters(self, mock_stack_manager: Mock, runner: CliRunner) -> None:
        """Test deploy command with custom parameters."""
        mock_instance = Mock()
        mock_instance.stack_name = "ZAEL-my-custom-stack"
        mock_instance.table_name = "ZAEL-my-custom-stack"
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
                "--name",
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
        assert "ZAEL-my-custom-stack" in result.output

    @patch("zae_limiter.cli.StackManager")
    def test_deploy_with_pitr_recovery_days(
        self, mock_stack_manager: Mock, runner: CliRunner
    ) -> None:
        """Test deploy command with --pitr-recovery-days parameter."""
        mock_instance = Mock()
        mock_instance.stack_name = "ZAEL-rate-limits"
        mock_instance.table_name = "ZAEL-rate-limits"
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
                "--pitr-recovery-days",
                "7",
                "--no-aggregator",
            ],
        )

        assert result.exit_code == 0
        # Verify create_stack was called with pitr_recovery_days parameter
        call_args = mock_instance.create_stack.call_args
        assert call_args is not None
        stack_options = call_args[1]["stack_options"]
        assert stack_options.pitr_recovery_days == 7

    @patch("zae_limiter.cli.StackManager")
    def test_deploy_with_log_retention_days(
        self, mock_stack_manager: Mock, runner: CliRunner
    ) -> None:
        """Test deploy command with --log-retention-days parameter."""
        mock_instance = Mock()
        mock_instance.stack_name = "ZAEL-rate-limits"
        mock_instance.table_name = "ZAEL-rate-limits"
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
                "--log-retention-days",
                "90",
                "--no-aggregator",
            ],
        )

        assert result.exit_code == 0
        # Verify create_stack was called with log_retention_days parameter
        call_args = mock_instance.create_stack.call_args
        assert call_args is not None
        stack_options = call_args[1]["stack_options"]
        assert stack_options.log_retention_days == 90

    @patch("zae_limiter.cli.StackManager")
    def test_deploy_with_lambda_timeout_and_memory(
        self, mock_stack_manager: Mock, runner: CliRunner
    ) -> None:
        """Test deploy command with --lambda-timeout and --lambda-memory parameters."""
        mock_instance = Mock()
        mock_instance.stack_name = "ZAEL-rate-limits"
        mock_instance.table_name = "ZAEL-rate-limits"
        mock_instance.create_stack = AsyncMock(
            return_value={
                "status": "CREATE_COMPLETE",
                "stack_id": "test-stack-id",
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

        result = runner.invoke(
            cli,
            [
                "deploy",
                "--lambda-timeout",
                "120",
                "--lambda-memory",
                "512",
            ],
        )

        assert result.exit_code == 0
        assert "Lambda timeout: 120s" in result.output
        assert "Lambda memory: 512MB" in result.output

        # Verify create_stack was called with correct parameters
        call_args = mock_instance.create_stack.call_args
        assert call_args is not None
        stack_options = call_args[1]["stack_options"]
        assert stack_options.lambda_timeout == 120
        assert stack_options.lambda_memory == 512

    @patch("zae_limiter.cli.StackManager")
    def test_deploy_with_alarms_disabled(self, mock_stack_manager: Mock, runner: CliRunner) -> None:
        """Test deploy command with --no-alarms parameter."""
        mock_instance = Mock()
        mock_instance.stack_name = "ZAEL-rate-limits"
        mock_instance.table_name = "ZAEL-rate-limits"
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
                "--no-alarms",
                "--no-aggregator",
            ],
        )

        assert result.exit_code == 0
        assert "Alarms: disabled" in result.output

        # Verify create_stack was called with enable_alarms=false
        call_args = mock_instance.create_stack.call_args
        assert call_args is not None
        stack_options = call_args[1]["stack_options"]
        assert stack_options.enable_alarms is False

    @patch("zae_limiter.cli.StackManager")
    def test_deploy_with_alarm_sns_topic(self, mock_stack_manager: Mock, runner: CliRunner) -> None:
        """Test deploy command with --alarm-sns-topic parameter."""
        mock_instance = Mock()
        mock_instance.stack_name = "ZAEL-rate-limits"
        mock_instance.table_name = "ZAEL-rate-limits"
        mock_instance.create_stack = AsyncMock(
            return_value={
                "status": "CREATE_COMPLETE",
                "stack_id": "test-stack-id",
            }
        )
        mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
        mock_instance.__aexit__ = AsyncMock(return_value=None)
        mock_stack_manager.return_value = mock_instance

        sns_topic = "arn:aws:sns:us-east-1:123456789012:my-topic"
        result = runner.invoke(
            cli,
            [
                "deploy",
                "--alarm-sns-topic",
                sns_topic,
                "--no-aggregator",
            ],
        )

        assert result.exit_code == 0
        assert f"Alarm SNS topic: {sns_topic}" in result.output

        # Verify create_stack was called with alarm_sns_topic
        call_args = mock_instance.create_stack.call_args
        assert call_args is not None
        stack_options = call_args[1]["stack_options"]
        assert stack_options.alarm_sns_topic == sns_topic

    @patch("zae_limiter.cli.StackManager")
    def test_deploy_with_lambda_duration_threshold_pct(
        self, mock_stack_manager: Mock, runner: CliRunner
    ) -> None:
        """Test deploy command with --lambda-duration-threshold-pct parameter."""
        mock_instance = Mock()
        mock_instance.stack_name = "ZAEL-rate-limits"
        mock_instance.table_name = "ZAEL-rate-limits"
        mock_instance.create_stack = AsyncMock(
            return_value={
                "status": "CREATE_COMPLETE",
                "stack_id": "test-stack-id",
            }
        )
        mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
        mock_instance.__aexit__ = AsyncMock(return_value=None)
        mock_stack_manager.return_value = mock_instance

        # Lambda timeout 60s with 50% threshold
        result = runner.invoke(
            cli,
            [
                "deploy",
                "--lambda-timeout",
                "60",
                "--lambda-duration-threshold-pct",
                "50",
                "--no-aggregator",
            ],
        )

        assert result.exit_code == 0

        # Verify create_stack was called with StackOptions having correct values
        call_args = mock_instance.create_stack.call_args
        assert call_args is not None
        stack_options = call_args[1]["stack_options"]
        assert stack_options.lambda_timeout == 60
        assert stack_options.lambda_duration_threshold_pct == 50

    @patch("zae_limiter.cli.StackManager")
    def test_deploy_duration_threshold_calculation(
        self, mock_stack_manager: Mock, runner: CliRunner
    ) -> None:
        """Test that duration threshold is correctly calculated from timeout and percentage."""
        mock_instance = Mock()
        mock_instance.stack_name = "ZAEL-rate-limits"
        mock_instance.table_name = "ZAEL-rate-limits"
        mock_instance.create_stack = AsyncMock(
            return_value={
                "status": "CREATE_COMPLETE",
                "stack_id": "test-stack-id",
            }
        )
        mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
        mock_instance.__aexit__ = AsyncMock(return_value=None)
        mock_stack_manager.return_value = mock_instance

        # Lambda timeout 120s with 80% threshold
        result = runner.invoke(
            cli,
            [
                "deploy",
                "--lambda-timeout",
                "120",
                "--lambda-duration-threshold-pct",
                "80",
                "--no-aggregator",
            ],
        )

        assert result.exit_code == 0

        call_args = mock_instance.create_stack.call_args
        assert call_args is not None
        stack_options = call_args[1]["stack_options"]
        assert stack_options.lambda_timeout == 120
        assert stack_options.lambda_duration_threshold_pct == 80
        # Verify to_parameters computes the ms value correctly (120s * 1000 * 0.8 = 96000ms)
        params = stack_options.to_parameters()
        assert params["lambda_duration_threshold"] == "96000"

    def test_deploy_lambda_timeout_invalid_range(self, runner: CliRunner) -> None:
        """Test that --lambda-timeout rejects values outside 1-900."""
        result = runner.invoke(
            cli,
            [
                "deploy",
                "--lambda-timeout",
                "0",
            ],
        )
        assert result.exit_code != 0
        # Click's IntRange provides error message with the invalid value
        assert "0" in result.output or "range" in result.output.lower()

        result = runner.invoke(
            cli,
            [
                "deploy",
                "--lambda-timeout",
                "901",
            ],
        )
        assert result.exit_code != 0

    def test_deploy_lambda_memory_invalid_range(self, runner: CliRunner) -> None:
        """Test that --lambda-memory rejects values outside 128-3008."""
        result = runner.invoke(
            cli,
            [
                "deploy",
                "--lambda-memory",
                "64",
            ],
        )
        assert result.exit_code != 0

        result = runner.invoke(
            cli,
            [
                "deploy",
                "--lambda-memory",
                "3009",
            ],
        )
        assert result.exit_code != 0

    def test_deploy_duration_threshold_pct_invalid_range(self, runner: CliRunner) -> None:
        """Test that --lambda-duration-threshold-pct rejects values outside 1-100."""
        result = runner.invoke(
            cli,
            [
                "deploy",
                "--lambda-duration-threshold-pct",
                "0",
            ],
        )
        assert result.exit_code != 0

        result = runner.invoke(
            cli,
            [
                "deploy",
                "--lambda-duration-threshold-pct",
                "101",
            ],
        )
        assert result.exit_code != 0

    @patch("zae_limiter.cli.StackManager")
    def test_deploy_lambda_skipped_local(self, mock_stack_manager: Mock, runner: CliRunner) -> None:
        """Test deploy shows correct message when Lambda deployment is skipped for local."""
        mock_instance = Mock()
        mock_instance.stack_name = "ZAEL-rate-limits"
        mock_instance.table_name = "ZAEL-rate-limits"
        mock_instance.create_stack = AsyncMock(
            return_value={
                "status": "CREATE_COMPLETE",
                "stack_id": "test-stack-id",
            }
        )
        # Lambda deployment returns skipped_local status
        mock_instance.deploy_lambda_code = AsyncMock(
            return_value={
                "status": "skipped_local",
            }
        )
        mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
        mock_instance.__aexit__ = AsyncMock(return_value=None)
        mock_stack_manager.return_value = mock_instance

        result = runner.invoke(cli, ["deploy"])

        assert result.exit_code == 0
        assert "Lambda deployment skipped (local environment)" in result.output

    @patch("zae_limiter.cli.StackManager")
    def test_deploy_with_endpoint_url(self, mock_stack_manager: Mock, runner: CliRunner) -> None:
        """Test deploy command with --endpoint-url for LocalStack."""
        mock_instance = Mock()
        mock_instance.stack_name = "ZAEL-test"
        mock_instance.table_name = "ZAEL-test"
        mock_instance.create_stack = AsyncMock(
            return_value={
                "status": "CREATE_COMPLETE",
                "stack_id": "test-stack-id",
            }
        )
        mock_instance.deploy_lambda_code = AsyncMock(
            return_value={
                "status": "deployed",
                "function_arn": "arn:aws:lambda:us-east-1:000000000000:function:test",
                "code_sha256": "abc123",
                "size_bytes": 40000,
            }
        )
        mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
        mock_instance.__aexit__ = AsyncMock(return_value=None)
        mock_stack_manager.return_value = mock_instance

        result = runner.invoke(
            cli,
            [
                "deploy",
                "--name",
                "test-table",
                "--endpoint-url",
                "http://localhost:4566",
                "--region",
                "us-east-1",
            ],
        )

        assert result.exit_code == 0
        # Verify StackManager was called with endpoint_url
        mock_stack_manager.assert_called_once_with(
            "test-table", "us-east-1", "http://localhost:4566"
        )

    @patch("zae_limiter.cli.StackManager")
    def test_deploy_skip_local(self, mock_stack_manager: Mock, runner: CliRunner) -> None:
        """Test deploy skips CloudFormation for local DynamoDB."""
        mock_instance = Mock()
        mock_instance.stack_name = "ZAEL-test-stack"
        mock_instance.table_name = "ZAEL-test-stack"
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
        result = runner.invoke(cli, ["delete", "--name", "test-stack"], input="n\n")
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

        result = runner.invoke(cli, ["delete", "--name", "test-stack", "--yes", "--wait"])

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

        result = runner.invoke(cli, ["delete", "--name", "test-stack", "--yes", "--no-wait"])

        assert result.exit_code == 0
        assert "initiated" in result.output

    @patch("zae_limiter.cli.StackManager")
    def test_delete_with_endpoint_url(self, mock_stack_manager: Mock, runner: CliRunner) -> None:
        """Test delete command with --endpoint-url for LocalStack."""
        mock_instance = Mock()
        mock_instance.delete_stack = AsyncMock(return_value=None)
        mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
        mock_instance.__aexit__ = AsyncMock(return_value=None)
        mock_stack_manager.return_value = mock_instance

        result = runner.invoke(
            cli,
            [
                "delete",
                "--name",
                "test-stack",
                "--endpoint-url",
                "http://localhost:4566",
                "--region",
                "us-east-1",
                "--yes",
            ],
        )

        assert result.exit_code == 0
        # Verify StackManager was called with endpoint_url
        mock_stack_manager.assert_called_once_with(
            "test-stack", "us-east-1", "http://localhost:4566"
        )

    @patch("zae_limiter.repository.Repository")
    @patch("zae_limiter.cli.StackManager")
    def test_status_exists(
        self, mock_stack_manager: Mock, mock_repository: Mock, runner: CliRunner
    ) -> None:
        """Test status command for existing stack."""
        # Mock StackManager
        mock_manager_instance = Mock()
        mock_manager_instance.get_stack_status = AsyncMock(return_value="CREATE_COMPLETE")
        mock_manager_instance.__aenter__ = AsyncMock(return_value=mock_manager_instance)
        mock_manager_instance.__aexit__ = AsyncMock(return_value=None)
        mock_stack_manager.return_value = mock_manager_instance

        # Mock Repository
        mock_repo_instance = Mock()
        mock_repo_instance._get_client = AsyncMock(
            return_value=Mock(
                describe_table=AsyncMock(
                    return_value={
                        "Table": {
                            "TableStatus": "ACTIVE",
                            "ItemCount": 100,
                            "TableSizeInBytes": 1024,
                            "StreamSpecification": {"StreamEnabled": True},
                        }
                    }
                )
            )
        )
        mock_repo_instance.get_version_record = AsyncMock(
            return_value={"schema_version": "1.0.0", "lambda_version": "0.1.0"}
        )
        mock_repo_instance.close = AsyncMock(return_value=None)
        mock_repository.return_value = mock_repo_instance

        result = runner.invoke(cli, ["status", "--name", "test-stack"])

        assert result.exit_code == 0
        assert "CREATE_COMPLETE" in result.output
        assert "✓ Infrastructure is ready" in result.output
        assert "Available:     ✓ Yes" in result.output

    @patch("zae_limiter.repository.Repository")
    @patch("zae_limiter.cli.StackManager")
    def test_status_not_found(
        self, mock_stack_manager: Mock, mock_repository: Mock, runner: CliRunner
    ) -> None:
        """Test status command for non-existent stack."""
        # Mock StackManager - stack doesn't exist
        mock_manager_instance = Mock()
        mock_manager_instance.get_stack_status = AsyncMock(return_value=None)
        mock_manager_instance.__aenter__ = AsyncMock(return_value=mock_manager_instance)
        mock_manager_instance.__aexit__ = AsyncMock(return_value=None)
        mock_stack_manager.return_value = mock_manager_instance

        # Mock Repository - table doesn't exist
        mock_repo_instance = Mock()
        mock_repo_instance._get_client = AsyncMock(
            return_value=Mock(describe_table=AsyncMock(side_effect=Exception("Table not found")))
        )
        mock_repo_instance.close = AsyncMock(return_value=None)
        mock_repository.return_value = mock_repo_instance

        result = runner.invoke(cli, ["status", "--name", "nonexistent"])

        assert result.exit_code == 1
        assert "Not found" in result.output
        assert "✗ Infrastructure is not available" in result.output

    @patch("zae_limiter.repository.Repository")
    @patch("zae_limiter.cli.StackManager")
    def test_status_in_progress(
        self, mock_stack_manager: Mock, mock_repository: Mock, runner: CliRunner
    ) -> None:
        """Test status command for stack in progress."""
        # Mock StackManager
        mock_manager_instance = Mock()
        mock_manager_instance.get_stack_status = AsyncMock(return_value="CREATE_IN_PROGRESS")
        mock_manager_instance.__aenter__ = AsyncMock(return_value=mock_manager_instance)
        mock_manager_instance.__aexit__ = AsyncMock(return_value=None)
        mock_stack_manager.return_value = mock_manager_instance

        # Mock Repository
        mock_repo_instance = Mock()
        mock_repo_instance._get_client = AsyncMock(
            return_value=Mock(
                describe_table=AsyncMock(
                    return_value={
                        "Table": {
                            "TableStatus": "ACTIVE",
                            "ItemCount": 0,
                            "TableSizeInBytes": 0,
                            "StreamSpecification": {"StreamEnabled": True},
                        }
                    }
                )
            )
        )
        mock_repo_instance.get_version_record = AsyncMock(return_value=None)
        mock_repo_instance.close = AsyncMock(return_value=None)
        mock_repository.return_value = mock_repo_instance

        result = runner.invoke(cli, ["status", "--name", "test-stack"])

        assert result.exit_code == 0
        assert "CREATE_IN_PROGRESS" in result.output
        assert "⏳" in result.output

    @patch("zae_limiter.repository.Repository")
    @patch("zae_limiter.cli.StackManager")
    def test_status_failed(
        self, mock_stack_manager: Mock, mock_repository: Mock, runner: CliRunner
    ) -> None:
        """Test status command for failed stack."""
        # Mock StackManager
        mock_manager_instance = Mock()
        mock_manager_instance.get_stack_status = AsyncMock(return_value="CREATE_FAILED")
        mock_manager_instance.__aenter__ = AsyncMock(return_value=mock_manager_instance)
        mock_manager_instance.__aexit__ = AsyncMock(return_value=None)
        mock_stack_manager.return_value = mock_manager_instance

        # Mock Repository - table might not exist after failed create
        mock_repo_instance = Mock()
        mock_repo_instance._get_client = AsyncMock(
            return_value=Mock(
                describe_table=AsyncMock(
                    return_value={
                        "Table": {
                            "TableStatus": "ACTIVE",
                            "ItemCount": 0,
                            "TableSizeInBytes": 0,
                        }
                    }
                )
            )
        )
        mock_repo_instance.get_version_record = AsyncMock(return_value=None)
        mock_repo_instance.close = AsyncMock(return_value=None)
        mock_repository.return_value = mock_repo_instance

        result = runner.invoke(cli, ["status", "--name", "test-stack"])

        assert result.exit_code == 1
        assert "CREATE_FAILED" in result.output
        assert "✗" in result.output

    def test_version_help(self, runner: CliRunner) -> None:
        """Test version command help."""
        result = runner.invoke(cli, ["version", "--help"])
        assert result.exit_code == 0
        assert "Show infrastructure version information" in result.output
        assert "--name" in result.output

    def test_upgrade_help(self, runner: CliRunner) -> None:
        """Test upgrade command help."""
        result = runner.invoke(cli, ["upgrade", "--help"])
        assert result.exit_code == 0
        assert "Upgrade infrastructure to match client version" in result.output
        assert "--name" in result.output
        assert "--lambda-only" in result.output
        assert "--force" in result.output

    def test_check_help(self, runner: CliRunner) -> None:
        """Test check command help."""
        result = runner.invoke(cli, ["check", "--help"])
        assert result.exit_code == 0
        assert "Check infrastructure compatibility" in result.output
        assert "--name" in result.output

    @patch("zae_limiter.repository.Repository")
    def test_version_not_initialized(self, mock_repo_class: Mock, runner: CliRunner) -> None:
        """Test version command when infrastructure not initialized."""
        mock_repo = Mock()
        mock_repo.get_version_record = AsyncMock(return_value=None)
        mock_repo.close = AsyncMock(return_value=None)
        mock_repo_class.return_value = mock_repo

        result = runner.invoke(cli, ["version", "--name", "test-table"])

        assert result.exit_code == 0
        assert "Not initialized" in result.output
        assert "zae-limiter deploy" in result.output

    @patch("zae_limiter.__version__", "1.0.0")
    @patch("zae_limiter.repository.Repository")
    def test_version_compatible(self, mock_repo_class: Mock, runner: CliRunner) -> None:
        """Test version command when versions are compatible."""
        mock_repo = Mock()
        mock_repo.get_version_record = AsyncMock(
            return_value={
                "schema_version": "1.0.0",
                "lambda_version": "1.0.0",
                "client_min_version": "0.0.0",
            }
        )
        mock_repo.close = AsyncMock(return_value=None)
        mock_repo_class.return_value = mock_repo

        result = runner.invoke(cli, ["version", "--name", "test-table"])

        assert result.exit_code == 0
        assert "Infrastructure Version" in result.output
        assert "Schema Version:" in result.output

    @patch("zae_limiter.repository.Repository")
    def test_version_with_endpoint_url(self, mock_repo_class: Mock, runner: CliRunner) -> None:
        """Test version command with --endpoint-url for LocalStack."""
        mock_repo = Mock()
        mock_repo.get_version_record = AsyncMock(return_value=None)
        mock_repo.close = AsyncMock(return_value=None)
        mock_repo_class.return_value = mock_repo

        result = runner.invoke(
            cli,
            [
                "version",
                "--name",
                "test-table",
                "--endpoint-url",
                "http://localhost:4566",
                "--region",
                "us-east-1",
            ],
        )

        assert result.exit_code == 0
        # Verify Repository was called with endpoint_url
        mock_repo_class.assert_called_once_with("test-table", "us-east-1", "http://localhost:4566")

    @patch("zae_limiter.repository.Repository")
    def test_check_not_initialized(self, mock_repo_class: Mock, runner: CliRunner) -> None:
        """Test check command when infrastructure not initialized."""
        mock_repo = Mock()
        mock_repo.get_version_record = AsyncMock(return_value=None)
        mock_repo.close = AsyncMock(return_value=None)
        mock_repo_class.return_value = mock_repo

        result = runner.invoke(cli, ["check", "--name", "test-table"])

        assert result.exit_code == 1
        assert "NOT INITIALIZED" in result.output

    @patch("zae_limiter.__version__", "1.0.0")
    @patch("zae_limiter.repository.Repository")
    def test_check_compatible(self, mock_repo_class: Mock, runner: CliRunner) -> None:
        """Test check command when compatible."""
        mock_repo = Mock()
        mock_repo.get_version_record = AsyncMock(
            return_value={
                "schema_version": "1.0.0",
                "lambda_version": "1.0.0",
                "client_min_version": "0.0.0",
            }
        )
        mock_repo.close = AsyncMock(return_value=None)
        mock_repo_class.return_value = mock_repo

        result = runner.invoke(cli, ["check", "--name", "test-table"])

        assert result.exit_code == 0
        assert "Compatibility Check" in result.output

    @patch("zae_limiter.repository.Repository")
    def test_check_with_endpoint_url(self, mock_repo_class: Mock, runner: CliRunner) -> None:
        """Test check command with --endpoint-url for LocalStack."""
        mock_repo = Mock()
        mock_repo.get_version_record = AsyncMock(return_value=None)
        mock_repo.close = AsyncMock(return_value=None)
        mock_repo_class.return_value = mock_repo

        result = runner.invoke(
            cli,
            [
                "check",
                "--name",
                "test-table",
                "--endpoint-url",
                "http://localhost:4566",
                "--region",
                "us-east-1",
            ],
        )

        assert result.exit_code == 1  # Not initialized
        # Verify Repository was called with endpoint_url
        mock_repo_class.assert_called_once_with("test-table", "us-east-1", "http://localhost:4566")

    @patch("zae_limiter.repository.Repository")
    def test_upgrade_not_initialized(self, mock_repo_class: Mock, runner: CliRunner) -> None:
        """Test upgrade command when infrastructure not initialized."""
        mock_repo = Mock()
        mock_repo.get_version_record = AsyncMock(return_value=None)
        mock_repo.close = AsyncMock(return_value=None)
        mock_repo_class.return_value = mock_repo

        result = runner.invoke(cli, ["upgrade", "--name", "test-table"])

        assert result.exit_code == 1
        assert "not initialized" in result.output.lower()
        assert "zae-limiter deploy" in result.output

    @patch("zae_limiter.repository.Repository")
    def test_upgrade_with_endpoint_url(self, mock_repo_class: Mock, runner: CliRunner) -> None:
        """Test upgrade command with --endpoint-url for LocalStack."""
        mock_repo = Mock()
        mock_repo.get_version_record = AsyncMock(return_value=None)
        mock_repo.close = AsyncMock(return_value=None)
        mock_repo_class.return_value = mock_repo

        result = runner.invoke(
            cli,
            [
                "upgrade",
                "--name",
                "test-table",
                "--endpoint-url",
                "http://localhost:4566",
                "--region",
                "us-east-1",
            ],
        )

        assert result.exit_code == 1  # Not initialized
        # Verify Repository was called with endpoint_url
        mock_repo_class.assert_called_once_with("test-table", "us-east-1", "http://localhost:4566")


class TestLambdaExport:
    """Test lambda-export command."""

    def test_lambda_export_help(self, runner: CliRunner) -> None:
        """Test lambda-export command help."""
        result = runner.invoke(cli, ["lambda-export", "--help"])
        assert result.exit_code == 0
        assert "Export Lambda deployment package" in result.output
        assert "--output" in result.output
        assert "--info" in result.output
        assert "--force" in result.output

    def test_lambda_export_info(self, runner: CliRunner) -> None:
        """Test lambda-export --info flag."""
        result = runner.invoke(cli, ["lambda-export", "--info"])
        assert result.exit_code == 0
        assert "Lambda Package Information" in result.output
        assert "Package path:" in result.output
        assert "Python files:" in result.output
        assert "Handler:" in result.output
        assert "zae_limiter.aggregator.handler.handler" in result.output

    def test_lambda_export_to_file(self, runner: CliRunner, tmp_path: Path) -> None:
        """Test exporting Lambda package to file."""
        import zipfile

        output_file = tmp_path / "test-lambda.zip"
        result = runner.invoke(cli, ["lambda-export", "--output", str(output_file)])

        assert result.exit_code == 0
        assert "Exported Lambda package to:" in result.output
        assert "KB" in result.output
        assert output_file.exists()

        # Verify it's a valid zip file
        with zipfile.ZipFile(output_file, "r") as zf:
            names = zf.namelist()
            assert any("zae_limiter" in name for name in names)
            assert any("handler.py" in name for name in names)

    def test_lambda_export_default_filename(self, runner: CliRunner) -> None:
        """Test lambda-export uses default filename."""
        # Use isolated filesystem to avoid polluting the project directory
        with runner.isolated_filesystem():
            result = runner.invoke(cli, ["lambda-export"])

            assert result.exit_code == 0
            assert "lambda.zip" in result.output
            assert Path("lambda.zip").exists()

    def test_lambda_export_refuses_overwrite(self, runner: CliRunner, tmp_path: Path) -> None:
        """Test lambda-export refuses to overwrite existing file."""
        output_file = tmp_path / "existing.zip"
        output_file.write_bytes(b"existing content")

        result = runner.invoke(cli, ["lambda-export", "--output", str(output_file)])

        assert result.exit_code == 1
        assert "File already exists" in result.output
        assert "--force" in result.output
        # Original file should be unchanged
        assert output_file.read_bytes() == b"existing content"

    def test_lambda_export_force_overwrite(self, runner: CliRunner, tmp_path: Path) -> None:
        """Test lambda-export with --force overwrites existing file."""
        output_file = tmp_path / "existing.zip"
        output_file.write_bytes(b"existing content")

        result = runner.invoke(cli, ["lambda-export", "--output", str(output_file), "--force"])

        assert result.exit_code == 0
        assert "Exported Lambda package to:" in result.output
        # File should be overwritten (different content)
        assert output_file.read_bytes() != b"existing content"

    def test_lambda_export_creates_parent_directory(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        """Test lambda-export creates parent directories if needed."""
        output_file = tmp_path / "nested" / "dirs" / "lambda.zip"
        result = runner.invoke(cli, ["lambda-export", "--output", str(output_file)])

        assert result.exit_code == 0
        assert output_file.exists()

    def test_lambda_export_short_flags(self, runner: CliRunner, tmp_path: Path) -> None:
        """Test lambda-export short flags -o and -f work."""
        output_file = tmp_path / "short-flag.zip"
        output_file.write_bytes(b"existing")

        result = runner.invoke(cli, ["lambda-export", "-o", str(output_file), "-f"])

        assert result.exit_code == 0
        assert output_file.exists()


class TestCLIValidationErrors:
    """Test CLI commands reject invalid names with helpful error messages."""

    def test_deploy_invalid_name_with_underscore(self, runner: CliRunner) -> None:
        """Test deploy rejects names with underscores."""
        result = runner.invoke(cli, ["deploy", "--name", "rate_limits"])
        assert result.exit_code == 1
        assert "underscore" in result.output.lower()

    def test_deploy_invalid_name_starts_with_number(self, runner: CliRunner) -> None:
        """Test deploy rejects names starting with numbers."""
        result = runner.invoke(cli, ["deploy", "--name", "123app"])
        assert result.exit_code == 1
        assert "error" in result.output.lower()

    def test_delete_invalid_name_with_underscore(self, runner: CliRunner) -> None:
        """Test delete rejects names with underscores."""
        result = runner.invoke(cli, ["delete", "--name", "rate_limits", "--yes"])
        assert result.exit_code == 1
        assert "underscore" in result.output.lower()

    def test_status_invalid_name_with_underscore(self, runner: CliRunner) -> None:
        """Test status rejects names with underscores."""
        result = runner.invoke(cli, ["status", "--name", "rate_limits"])
        assert result.exit_code == 1
        assert "underscore" in result.output.lower()

    def test_version_invalid_name_with_underscore(self, runner: CliRunner) -> None:
        """Test version rejects names with underscores."""
        result = runner.invoke(cli, ["version", "--name", "rate_limits"])
        assert result.exit_code == 1
        assert "underscore" in result.output.lower()

    def test_check_invalid_name_with_underscore(self, runner: CliRunner) -> None:
        """Test check rejects names with underscores."""
        result = runner.invoke(cli, ["check", "--name", "rate_limits"])
        assert result.exit_code == 1
        assert "underscore" in result.output.lower()

    def test_upgrade_invalid_name_with_underscore(self, runner: CliRunner) -> None:
        """Test upgrade rejects names with underscores."""
        result = runner.invoke(cli, ["upgrade", "--name", "rate_limits"])
        assert result.exit_code == 1
        assert "underscore" in result.output.lower()

    def test_deploy_invalid_name_with_period(self, runner: CliRunner) -> None:
        """Test deploy rejects names with periods."""
        result = runner.invoke(cli, ["deploy", "--name", "my.app"])
        assert result.exit_code == 1
        assert "period" in result.output.lower()

    def test_deploy_invalid_name_with_space(self, runner: CliRunner) -> None:
        """Test deploy rejects names with spaces."""
        result = runner.invoke(cli, ["deploy", "--name", "my app"])
        assert result.exit_code == 1
        assert "space" in result.output.lower()
