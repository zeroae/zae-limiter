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
        # IAM options
        assert "--iam" in result.output
        assert "--no-iam" in result.output
        assert "--aggregator-role-arn" in result.output

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

    @patch("zae_limiter.repository.Repository")
    @patch("zae_limiter.cli.StackManager")
    def test_deploy_default_parameters(
        self, mock_stack_manager: Mock, mock_repository: Mock, runner: CliRunner
    ) -> None:
        """Test deploy command with default parameters."""
        # Mock stack manager
        mock_instance = Mock()
        mock_instance.stack_name = "rate-limits"
        mock_instance.table_name = "rate-limits"
        mock_instance.create_stack = AsyncMock(
            return_value={
                "status": "CREATE_COMPLETE",
                "stack_id": "test-stack-id",
                "stack_name": "rate-limits",
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

        # Mock repository for version record
        mock_repo_instance = Mock()
        mock_repo_instance.set_version_record = AsyncMock()
        mock_repository.return_value = mock_repo_instance

        result = runner.invoke(cli, ["deploy"])

        assert result.exit_code == 0
        assert "Deploying stack: rate-limits" in result.output
        assert "✓" in result.output
        assert "Version record initialized" in result.output

        # Verify default values for new parameters via StackOptions
        call_args = mock_instance.create_stack.call_args
        assert call_args is not None
        stack_options = call_args[1]["stack_options"]
        # Default values: lambda_timeout=60, lambda_memory=256, alarms enabled, 80% threshold
        assert stack_options.lambda_timeout == 60
        assert stack_options.lambda_memory == 256
        assert stack_options.enable_alarms is True
        assert stack_options.lambda_duration_threshold_pct == 80

        # Verify version record was created
        mock_repo_instance.set_version_record.assert_called_once()
        version_call_args = mock_repo_instance.set_version_record.call_args
        assert version_call_args[1]["schema_version"] == "0.8.0"
        assert version_call_args[1]["client_min_version"] == "0.0.0"

    @patch("zae_limiter.repository.Repository")
    @patch("zae_limiter.cli.StackManager")
    def test_deploy_custom_parameters(
        self, mock_stack_manager: Mock, mock_repository: Mock, runner: CliRunner
    ) -> None:
        """Test deploy command with custom parameters."""
        mock_instance = Mock()
        mock_instance.stack_name = "my-custom-stack"
        mock_instance.table_name = "my-custom-stack"
        mock_instance.create_stack = AsyncMock(
            return_value={
                "status": "CREATE_COMPLETE",
                "stack_id": "test-stack-id",
            }
        )
        mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
        mock_instance.__aexit__ = AsyncMock(return_value=None)
        mock_stack_manager.return_value = mock_instance

        # Mock repository for version record
        mock_repo_instance = Mock()
        mock_repo_instance.set_version_record = AsyncMock()
        mock_repository.return_value = mock_repo_instance

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
                "--usage-retention-days",
                "30",
                "--no-aggregator",
            ],
        )

        assert result.exit_code == 0
        assert "my-custom-stack" in result.output

    @patch("zae_limiter.repository.Repository")
    @patch("zae_limiter.cli.StackManager")
    def test_deploy_with_pitr_recovery_days(
        self, mock_stack_manager: Mock, mock_repository: Mock, runner: CliRunner
    ) -> None:
        """Test deploy command with --pitr-recovery-days parameter."""
        mock_instance = Mock()
        mock_instance.stack_name = "rate-limits"
        mock_instance.table_name = "rate-limits"
        mock_instance.create_stack = AsyncMock(
            return_value={
                "status": "CREATE_COMPLETE",
                "stack_id": "test-stack-id",
            }
        )
        mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
        mock_instance.__aexit__ = AsyncMock(return_value=None)
        mock_stack_manager.return_value = mock_instance

        # Mock repository for version record
        mock_repo_instance = Mock()
        mock_repo_instance.set_version_record = AsyncMock()
        mock_repository.return_value = mock_repo_instance

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

    @patch("zae_limiter.repository.Repository")
    @patch("zae_limiter.cli.StackManager")
    def test_deploy_with_log_retention_days(
        self, mock_stack_manager: Mock, mock_repository: Mock, runner: CliRunner
    ) -> None:
        """Test deploy command with --log-retention-days parameter."""
        mock_instance = Mock()
        mock_instance.stack_name = "rate-limits"
        mock_instance.table_name = "rate-limits"
        mock_instance.create_stack = AsyncMock(
            return_value={
                "status": "CREATE_COMPLETE",
                "stack_id": "test-stack-id",
            }
        )
        mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
        mock_instance.__aexit__ = AsyncMock(return_value=None)
        mock_stack_manager.return_value = mock_instance

        # Mock repository for version record
        mock_repo_instance = Mock()
        mock_repo_instance.set_version_record = AsyncMock()
        mock_repository.return_value = mock_repo_instance

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

    @patch("zae_limiter.repository.Repository")
    @patch("zae_limiter.cli.StackManager")
    def test_deploy_with_lambda_timeout_and_memory(
        self, mock_stack_manager: Mock, mock_repository: Mock, runner: CliRunner
    ) -> None:
        """Test deploy command with --lambda-timeout and --lambda-memory parameters."""
        mock_instance = Mock()
        mock_instance.stack_name = "rate-limits"
        mock_instance.table_name = "rate-limits"
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

        # Mock repository for version record
        mock_repo_instance = Mock()
        mock_repo_instance.set_version_record = AsyncMock()
        mock_repository.return_value = mock_repo_instance

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

    @patch("zae_limiter.repository.Repository")
    @patch("zae_limiter.cli.StackManager")
    def test_deploy_with_alarms_disabled(
        self, mock_stack_manager: Mock, mock_repository: Mock, runner: CliRunner
    ) -> None:
        """Test deploy command with --no-alarms parameter."""
        mock_instance = Mock()
        mock_instance.stack_name = "rate-limits"
        mock_instance.table_name = "rate-limits"
        mock_instance.create_stack = AsyncMock(
            return_value={
                "status": "CREATE_COMPLETE",
                "stack_id": "test-stack-id",
            }
        )
        mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
        mock_instance.__aexit__ = AsyncMock(return_value=None)
        mock_stack_manager.return_value = mock_instance

        # Mock repository for version record
        mock_repo_instance = Mock()
        mock_repo_instance.set_version_record = AsyncMock()
        mock_repository.return_value = mock_repo_instance

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

    @patch("zae_limiter.repository.Repository")
    @patch("zae_limiter.cli.StackManager")
    def test_deploy_with_alarm_sns_topic(
        self, mock_stack_manager: Mock, mock_repository: Mock, runner: CliRunner
    ) -> None:
        """Test deploy command with --alarm-sns-topic parameter."""
        mock_instance = Mock()
        mock_instance.stack_name = "rate-limits"
        mock_instance.table_name = "rate-limits"
        mock_instance.create_stack = AsyncMock(
            return_value={
                "status": "CREATE_COMPLETE",
                "stack_id": "test-stack-id",
            }
        )
        mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
        mock_instance.__aexit__ = AsyncMock(return_value=None)
        mock_stack_manager.return_value = mock_instance

        # Mock repository for version record
        mock_repo_instance = Mock()
        mock_repo_instance.set_version_record = AsyncMock()
        mock_repository.return_value = mock_repo_instance

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

    @patch("zae_limiter.repository.Repository")
    @patch("zae_limiter.cli.StackManager")
    def test_deploy_with_lambda_duration_threshold_pct(
        self, mock_stack_manager: Mock, mock_repository: Mock, runner: CliRunner
    ) -> None:
        """Test deploy command with --lambda-duration-threshold-pct parameter."""
        mock_instance = Mock()
        mock_instance.stack_name = "rate-limits"
        mock_instance.table_name = "rate-limits"
        mock_instance.create_stack = AsyncMock(
            return_value={
                "status": "CREATE_COMPLETE",
                "stack_id": "test-stack-id",
            }
        )
        mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
        mock_instance.__aexit__ = AsyncMock(return_value=None)
        mock_stack_manager.return_value = mock_instance

        # Mock repository for version record
        mock_repo_instance = Mock()
        mock_repo_instance.set_version_record = AsyncMock()
        mock_repository.return_value = mock_repo_instance

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

    @patch("zae_limiter.repository.Repository")
    @patch("zae_limiter.cli.StackManager")
    def test_deploy_duration_threshold_calculation(
        self, mock_stack_manager: Mock, mock_repository: Mock, runner: CliRunner
    ) -> None:
        """Test that duration threshold is correctly calculated from timeout and percentage."""
        mock_instance = Mock()
        mock_instance.stack_name = "rate-limits"
        mock_instance.table_name = "rate-limits"
        mock_instance.create_stack = AsyncMock(
            return_value={
                "status": "CREATE_COMPLETE",
                "stack_id": "test-stack-id",
            }
        )
        mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
        mock_instance.__aexit__ = AsyncMock(return_value=None)
        mock_stack_manager.return_value = mock_instance

        # Mock repository for version record
        mock_repo_instance = Mock()
        mock_repo_instance.set_version_record = AsyncMock()
        mock_repository.return_value = mock_repo_instance

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

    @patch("zae_limiter.repository.Repository")
    @patch("zae_limiter.cli.StackManager")
    def test_deploy_with_endpoint_url(
        self, mock_stack_manager: Mock, mock_repository: Mock, runner: CliRunner
    ) -> None:
        """Test deploy command with --endpoint-url for LocalStack."""
        mock_instance = Mock()
        mock_instance.stack_name = "test"
        mock_instance.table_name = "test"
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

        # Mock repository for version record
        mock_repo_instance = Mock()
        mock_repo_instance.set_version_record = AsyncMock()
        mock_repository.return_value = mock_repo_instance

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

    @patch("zae_limiter.repository.Repository")
    @patch("zae_limiter.cli.StackManager")
    def test_deploy_with_tags(
        self, mock_stack_manager: Mock, mock_repository: Mock, runner: CliRunner
    ) -> None:
        """Test deploy command with --tag options."""
        mock_instance = Mock()
        mock_instance.stack_name = "rate-limits"
        mock_instance.table_name = "rate-limits"
        mock_instance.create_stack = AsyncMock(
            return_value={
                "status": "CREATE_COMPLETE",
                "stack_id": "test-stack-id",
            }
        )
        mock_instance.deploy_lambda_code = AsyncMock(
            return_value={
                "status": "deployed",
                "function_arn": "arn:aws:lambda:us-east-1:123:function:test",
                "code_sha256": "abc123",
                "size_bytes": 30000,
            }
        )
        mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
        mock_instance.__aexit__ = AsyncMock(return_value=None)
        mock_stack_manager.return_value = mock_instance

        mock_repo_instance = Mock()
        mock_repo_instance.set_version_record = AsyncMock()
        mock_repository.return_value = mock_repo_instance

        result = runner.invoke(
            cli,
            [
                "deploy",
                "--tag",
                "env=prod",
                "--tag",
                "team=platform",
                "--no-aggregator",
            ],
        )

        assert result.exit_code == 0
        call_args = mock_instance.create_stack.call_args
        assert call_args is not None
        stack_options = call_args[1]["stack_options"]
        assert stack_options.tags == {"env": "prod", "team": "platform"}

    @patch("zae_limiter.repository.Repository")
    @patch("zae_limiter.cli.StackManager")
    def test_deploy_with_tags_displays_output(
        self, mock_stack_manager: Mock, mock_repository: Mock, runner: CliRunner
    ) -> None:
        """Test deploy command displays user-defined tags."""
        mock_instance = Mock()
        mock_instance.stack_name = "rate-limits"
        mock_instance.table_name = "rate-limits"
        mock_instance.create_stack = AsyncMock(
            return_value={
                "status": "CREATE_COMPLETE",
                "stack_id": "test-stack-id",
            }
        )
        mock_instance.deploy_lambda_code = AsyncMock(
            return_value={
                "status": "deployed",
                "function_arn": "arn:aws:lambda:us-east-1:123:function:test",
                "code_sha256": "abc123",
                "size_bytes": 30000,
            }
        )
        mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
        mock_instance.__aexit__ = AsyncMock(return_value=None)
        mock_stack_manager.return_value = mock_instance

        mock_repo_instance = Mock()
        mock_repo_instance.set_version_record = AsyncMock()
        mock_repository.return_value = mock_repo_instance

        result = runner.invoke(
            cli,
            ["deploy", "--tag", "env=prod", "--no-aggregator"],
        )

        assert result.exit_code == 0
        assert "1 user-defined" in result.output
        assert "env=prod" in result.output

    def test_deploy_with_invalid_tag_format(self, runner: CliRunner) -> None:
        """Test deploy command rejects tags without KEY=VALUE format."""
        result = runner.invoke(cli, ["deploy", "--tag", "invalid-tag"])

        assert result.exit_code != 0

    @patch("zae_limiter.repository.Repository")
    @patch("zae_limiter.cli.StackManager")
    def test_deploy_with_no_iam_flag(
        self, mock_stack_manager: Mock, mock_repository: Mock, runner: CliRunner
    ) -> None:
        """Test deploy command with --no-iam flag."""
        mock_instance = Mock()
        mock_instance.stack_name = "test-stack"
        mock_instance.table_name = "test-stack"
        mock_instance.create_stack = AsyncMock(
            return_value={
                "status": "CREATE_COMPLETE",
                "stack_id": "test-stack-id",
            }
        )
        mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
        mock_instance.__aexit__ = AsyncMock(return_value=None)
        mock_stack_manager.return_value = mock_instance

        mock_repo_instance = Mock()
        mock_repo_instance.set_version_record = AsyncMock()
        mock_repository.return_value = mock_repo_instance

        result = runner.invoke(
            cli,
            ["deploy", "--name", "test-stack", "--no-iam", "--no-aggregator"],
        )

        assert result.exit_code == 0
        # Verify create_iam is False in StackOptions
        call_args = mock_instance.create_stack.call_args
        stack_options = call_args[1]["stack_options"]
        assert stack_options.create_iam is False

    @patch("zae_limiter.repository.Repository")
    @patch("zae_limiter.cli.StackManager")
    def test_deploy_with_aggregator_role_arn(
        self, mock_stack_manager: Mock, mock_repository: Mock, runner: CliRunner
    ) -> None:
        """Test deploy command with --aggregator-role-arn flag."""
        mock_instance = Mock()
        mock_instance.stack_name = "test-stack"
        mock_instance.table_name = "test-stack"
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
                "code_sha256": "abc123def456ghi789",
                "size_bytes": 30000,
            }
        )
        mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
        mock_instance.__aexit__ = AsyncMock(return_value=None)
        mock_stack_manager.return_value = mock_instance

        mock_repo_instance = Mock()
        mock_repo_instance.set_version_record = AsyncMock()
        mock_repository.return_value = mock_repo_instance

        role_arn = "arn:aws:iam::123456789012:role/MyLambdaRole"
        result = runner.invoke(
            cli,
            ["deploy", "--name", "test-stack", "--aggregator-role-arn", role_arn],
        )

        assert result.exit_code == 0
        call_args = mock_instance.create_stack.call_args
        stack_options = call_args[1]["stack_options"]
        assert stack_options.aggregator_role_arn == role_arn

    @patch("zae_limiter.repository.Repository")
    @patch("zae_limiter.cli.StackManager")
    def test_deploy_with_no_iam_and_aggregator_role_arn(
        self, mock_stack_manager: Mock, mock_repository: Mock, runner: CliRunner
    ) -> None:
        """Test deploy with --no-iam and --aggregator-role-arn enables aggregator."""
        mock_instance = Mock()
        mock_instance.stack_name = "test-stack"
        mock_instance.table_name = "test-stack"
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
                "code_sha256": "abc123def456ghi789",
                "size_bytes": 30000,
            }
        )
        mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
        mock_instance.__aexit__ = AsyncMock(return_value=None)
        mock_stack_manager.return_value = mock_instance

        mock_repo_instance = Mock()
        mock_repo_instance.set_version_record = AsyncMock()
        mock_repository.return_value = mock_repo_instance

        role_arn = "arn:aws:iam::123456789012:role/MyLambdaRole"
        result = runner.invoke(
            cli,
            [
                "deploy",
                "--name",
                "test-stack",
                "--no-iam",
                "--aggregator-role-arn",
                role_arn,
            ],
        )

        assert result.exit_code == 0
        call_args = mock_instance.create_stack.call_args
        stack_options = call_args[1]["stack_options"]
        assert stack_options.create_iam is False
        assert stack_options.aggregator_role_arn == role_arn
        # Aggregator should be enabled since external role is provided
        assert stack_options.enable_aggregator is True

    def test_deploy_no_iam_with_create_iam_roles_errors(self, runner: CliRunner) -> None:
        """Test deploy command errors with --no-iam and --create-iam-roles."""
        result = runner.invoke(
            cli,
            ["deploy", "--name", "test-stack", "--no-iam", "--create-iam-roles"],
        )

        assert result.exit_code != 0
        assert "--create-iam-roles cannot be used with --no-iam" in result.output

    def test_deploy_no_iam_disables_aggregator(self, runner: CliRunner) -> None:
        """Test --no-iam without external role shows note and disables aggregator."""
        # Run without mocking to see the early validation message
        result = runner.invoke(
            cli,
            ["deploy", "--name", "test-stack", "--no-iam"],
            catch_exceptions=False,
        )

        # Should show note about disabling aggregator
        assert "--no-iam disables aggregator" in result.output

    def test_deploy_aggregator_role_arn_invalid_format(self, runner: CliRunner) -> None:
        """Test deploy rejects invalid IAM role ARN format."""
        result = runner.invoke(
            cli,
            [
                "deploy",
                "--name",
                "test-stack",
                "--aggregator-role-arn",
                "invalid-arn",
            ],
        )

        assert result.exit_code != 0
        # Validation error raised from StackOptions model
        assert result.exception is not None
        assert "must be a valid IAM role ARN" in str(result.exception)

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


class TestUpgradeEnsureTags:
    """Tests for ensure_tags during upgrade."""

    @patch("zae_limiter.__version__", "1.1.0")
    @patch("zae_limiter.cli.StackManager")
    @patch("zae_limiter.repository.Repository")
    def test_upgrade_calls_ensure_tags(
        self, mock_repo_class: Mock, mock_stack_manager: Mock, runner: CliRunner
    ) -> None:
        """Upgrade command calls ensure_tags on the stack."""
        mock_repo = Mock()
        mock_repo.get_version_record = AsyncMock(
            return_value={
                "schema_version": "1.0.0",
                "lambda_version": "1.0.0",
                "client_min_version": "0.0.0",
            }
        )
        mock_repo.set_version_record = AsyncMock()
        mock_repo.close = AsyncMock(return_value=None)
        mock_repo_class.return_value = mock_repo

        mock_manager = Mock()
        mock_manager.deploy_lambda_code = AsyncMock(
            return_value={"status": "deployed", "size_bytes": 30000}
        )
        mock_manager.ensure_tags = AsyncMock(return_value=True)
        mock_manager.__aenter__ = AsyncMock(return_value=mock_manager)
        mock_manager.__aexit__ = AsyncMock(return_value=None)
        mock_stack_manager.return_value = mock_manager

        result = runner.invoke(
            cli,
            ["upgrade", "--name", "test-app", "--force"],
        )

        assert result.exit_code == 0
        mock_manager.ensure_tags.assert_called_once()
        assert "Discovery tags added" in result.output

    @patch("zae_limiter.__version__", "1.1.0")
    @patch("zae_limiter.cli.StackManager")
    @patch("zae_limiter.repository.Repository")
    def test_upgrade_ensure_tags_already_present(
        self, mock_repo_class: Mock, mock_stack_manager: Mock, runner: CliRunner
    ) -> None:
        """Upgrade command shows tags already present."""
        mock_repo = Mock()
        mock_repo.get_version_record = AsyncMock(
            return_value={
                "schema_version": "1.0.0",
                "lambda_version": "1.0.0",
                "client_min_version": "0.0.0",
            }
        )
        mock_repo.set_version_record = AsyncMock()
        mock_repo.close = AsyncMock(return_value=None)
        mock_repo_class.return_value = mock_repo

        mock_manager = Mock()
        mock_manager.deploy_lambda_code = AsyncMock(
            return_value={"status": "deployed", "size_bytes": 30000}
        )
        mock_manager.ensure_tags = AsyncMock(return_value=False)
        mock_manager.__aenter__ = AsyncMock(return_value=mock_manager)
        mock_manager.__aexit__ = AsyncMock(return_value=None)
        mock_stack_manager.return_value = mock_manager

        result = runner.invoke(
            cli,
            ["upgrade", "--name", "test-app", "--force"],
        )

        assert result.exit_code == 0
        assert "Tags already present" in result.output

    @patch("zae_limiter.__version__", "1.1.0")
    @patch("zae_limiter.cli.StackManager")
    @patch("zae_limiter.repository.Repository")
    def test_upgrade_ensure_tags_failure_non_fatal(
        self, mock_repo_class: Mock, mock_stack_manager: Mock, runner: CliRunner
    ) -> None:
        """Upgrade continues when ensure_tags fails."""
        mock_repo = Mock()
        mock_repo.get_version_record = AsyncMock(
            return_value={
                "schema_version": "1.0.0",
                "lambda_version": "1.0.0",
                "client_min_version": "0.0.0",
            }
        )
        mock_repo.set_version_record = AsyncMock()
        mock_repo.close = AsyncMock(return_value=None)
        mock_repo_class.return_value = mock_repo

        mock_manager = Mock()
        mock_manager.deploy_lambda_code = AsyncMock(
            return_value={"status": "deployed", "size_bytes": 30000}
        )
        mock_manager.ensure_tags = AsyncMock(side_effect=Exception("Access denied"))
        mock_manager.__aenter__ = AsyncMock(return_value=mock_manager)
        mock_manager.__aexit__ = AsyncMock(return_value=None)
        mock_stack_manager.return_value = mock_manager

        result = runner.invoke(
            cli,
            ["upgrade", "--name", "test-app", "--force"],
        )

        assert result.exit_code == 0
        assert "Tag update failed" in result.output
        assert "Upgrade complete" in result.output


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
        assert "zae_limiter_aggregator.handler.handler" in result.output

    def test_lambda_export_to_file(self, runner: CliRunner, tmp_path: Path) -> None:
        """Test exporting Lambda package to file."""
        import zipfile
        from unittest.mock import patch

        def _mock_write(output_path: Path) -> int:
            """Write a minimal valid zip for testing."""
            import io

            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w") as zf:
                zf.writestr("zae_limiter/__init__.py", "")
                zf.writestr("zae_limiter/aggregator/handler.py", "")
            data = buf.getvalue()
            Path(output_path).write_bytes(data)
            return len(data)

        output_file = tmp_path / "test-lambda.zip"
        with patch("zae_limiter.cli.write_lambda_package", side_effect=_mock_write):
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
        from unittest.mock import patch

        def _mock_write(output_path: Path) -> int:
            import io
            import zipfile

            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w") as zf:
                zf.writestr("zae_limiter/__init__.py", "")
            data = buf.getvalue()
            Path(output_path).write_bytes(data)
            return len(data)

        with runner.isolated_filesystem():
            with patch("zae_limiter.cli.write_lambda_package", side_effect=_mock_write):
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
        from unittest.mock import patch

        def _mock_write(output_path: Path) -> int:
            import io
            import zipfile

            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w") as zf:
                zf.writestr("zae_limiter/__init__.py", "")
            data = buf.getvalue()
            Path(output_path).write_bytes(data)
            return len(data)

        output_file = tmp_path / "existing.zip"
        output_file.write_bytes(b"existing content")

        with patch("zae_limiter.cli.write_lambda_package", side_effect=_mock_write):
            result = runner.invoke(cli, ["lambda-export", "--output", str(output_file), "--force"])

        assert result.exit_code == 0
        assert "Exported Lambda package to:" in result.output
        # File should be overwritten (different content)
        assert output_file.read_bytes() != b"existing content"

    def test_lambda_export_creates_parent_directory(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        """Test lambda-export creates parent directories if needed."""
        from unittest.mock import patch

        def _mock_write(output_path: Path) -> int:
            import io
            import zipfile

            Path(output_path).parent.mkdir(parents=True, exist_ok=True)
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w") as zf:
                zf.writestr("zae_limiter/__init__.py", "")
            data = buf.getvalue()
            Path(output_path).write_bytes(data)
            return len(data)

        output_file = tmp_path / "nested" / "dirs" / "lambda.zip"
        with patch("zae_limiter.cli.write_lambda_package", side_effect=_mock_write):
            result = runner.invoke(cli, ["lambda-export", "--output", str(output_file)])

        assert result.exit_code == 0
        assert output_file.exists()

    def test_lambda_export_short_flags(self, runner: CliRunner, tmp_path: Path) -> None:
        """Test lambda-export short flags -o and -f work."""
        from unittest.mock import patch

        def _mock_write(output_path: Path) -> int:
            import io
            import zipfile

            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w") as zf:
                zf.writestr("zae_limiter/__init__.py", "")
            data = buf.getvalue()
            Path(output_path).write_bytes(data)
            return len(data)

        output_file = tmp_path / "short-flag.zip"
        output_file.write_bytes(b"existing")

        with patch("zae_limiter.cli.write_lambda_package", side_effect=_mock_write):
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

    def test_audit_list_invalid_name_with_underscore(self, runner: CliRunner) -> None:
        """Test audit list rejects names with underscores."""
        result = runner.invoke(cli, ["audit", "list", "--name", "rate_limits", "-e", "test"])
        assert result.exit_code == 1
        assert "underscore" in result.output.lower()


class TestAuditCommands:
    """Test audit CLI commands."""

    def test_audit_help(self, runner: CliRunner) -> None:
        """Test audit command group help."""
        result = runner.invoke(cli, ["audit", "--help"])
        assert result.exit_code == 0
        assert "Audit log commands" in result.output

    def test_audit_list_help(self, runner: CliRunner) -> None:
        """Test audit list command help."""
        result = runner.invoke(cli, ["audit", "list", "--help"])
        assert result.exit_code == 0
        assert "List audit events for an entity" in result.output
        assert "--entity-id" in result.output
        assert "--limit" in result.output
        assert "--start-event-id" in result.output

    def test_audit_list_requires_entity_id(self, runner: CliRunner) -> None:
        """Test audit list requires --entity-id."""
        result = runner.invoke(cli, ["audit", "list"])
        assert result.exit_code != 0
        assert "entity-id" in result.output.lower()

    @patch("zae_limiter.repository.Repository")
    def test_audit_list_no_events(self, mock_repo_class: Mock, runner: CliRunner) -> None:
        """Test audit list when no events are found."""
        mock_repo = Mock()
        mock_repo.get_audit_events = AsyncMock(return_value=[])
        mock_repo.close = AsyncMock(return_value=None)
        mock_repo_class.return_value = mock_repo

        result = runner.invoke(cli, ["audit", "list", "-e", "test-entity"])

        assert result.exit_code == 0
        assert "No audit events found for entity: test-entity" in result.output

    @patch("zae_limiter.repository.Repository")
    def test_audit_list_with_events(self, mock_repo_class: Mock, runner: CliRunner) -> None:
        """Test audit list displays events in table format."""
        from zae_limiter.models import AuditEvent

        mock_events = [
            AuditEvent(
                event_id="01ABCDEF",
                timestamp="2025-01-16T12:00:00Z",
                action="entity_created",
                entity_id="test-entity",
                principal="admin@example.com",
                resource=None,
            ),
            AuditEvent(
                event_id="01ABCDEG",
                timestamp="2025-01-16T12:01:00Z",
                action="limits_set",
                entity_id="test-entity",
                principal="admin@example.com",
                resource="api-calls",
            ),
        ]

        mock_repo = Mock()
        mock_repo.get_audit_events = AsyncMock(return_value=mock_events)
        mock_repo.close = AsyncMock(return_value=None)
        mock_repo_class.return_value = mock_repo

        result = runner.invoke(cli, ["audit", "list", "-e", "test-entity"])

        assert result.exit_code == 0
        assert "Audit Events for: test-entity" in result.output
        assert "Timestamp" in result.output
        assert "Action" in result.output
        assert "Principal" in result.output
        assert "Resource" in result.output
        assert "entity_created" in result.output
        assert "limits_set" in result.output
        assert "admin@example.com" in result.output
        assert "api-calls" in result.output
        assert "Total: 2 events" in result.output

    @patch("zae_limiter.repository.Repository")
    def test_audit_list_shows_long_principal_in_full(
        self, mock_repo_class: Mock, runner: CliRunner
    ) -> None:
        """Test audit list shows long principal in full (auto-sized columns)."""
        from zae_limiter.models import AuditEvent

        long_principal = "arn:aws:iam::123456789012:user/very-long-username-that-exceeds-limit"
        mock_events = [
            AuditEvent(
                event_id="01ABCDEF",
                timestamp="2025-01-16T12:00:00Z",
                action="entity_created",
                entity_id="test-entity",
                principal=long_principal,
                resource=None,
            ),
        ]

        mock_repo = Mock()
        mock_repo.get_audit_events = AsyncMock(return_value=mock_events)
        mock_repo.close = AsyncMock(return_value=None)
        mock_repo_class.return_value = mock_repo

        result = runner.invoke(cli, ["audit", "list", "-e", "test-entity"])

        assert result.exit_code == 0
        # Full principal shown in auto-sized table
        assert long_principal in result.output
        # Box-drawing table format
        assert "+-" in result.output

    @patch("zae_limiter.repository.Repository")
    def test_audit_list_shows_pagination_hint(
        self, mock_repo_class: Mock, runner: CliRunner
    ) -> None:
        """Test audit list shows pagination hint when limit is reached."""
        from zae_limiter.models import AuditEvent

        # Create exactly 5 events (matches limit)
        mock_events = [
            AuditEvent(
                event_id=f"01ABCDE{i}",
                timestamp="2025-01-16T12:00:00Z",
                action="entity_created",
                entity_id="test-entity",
                principal="admin@example.com",
                resource=None,
            )
            for i in range(5)
        ]

        mock_repo = Mock()
        mock_repo.get_audit_events = AsyncMock(return_value=mock_events)
        mock_repo.close = AsyncMock(return_value=None)
        mock_repo_class.return_value = mock_repo

        result = runner.invoke(cli, ["audit", "list", "-e", "test-entity", "-l", "5"])

        assert result.exit_code == 0
        assert "More events may exist" in result.output
        assert "--start-event-id" in result.output

    @patch("zae_limiter.repository.Repository")
    def test_audit_list_with_custom_limit(self, mock_repo_class: Mock, runner: CliRunner) -> None:
        """Test audit list with custom limit."""
        mock_repo = Mock()
        mock_repo.get_audit_events = AsyncMock(return_value=[])
        mock_repo.close = AsyncMock(return_value=None)
        mock_repo_class.return_value = mock_repo

        result = runner.invoke(cli, ["audit", "list", "-e", "test-entity", "--limit", "50"])

        assert result.exit_code == 0
        mock_repo.get_audit_events.assert_called_once_with(
            entity_id="test-entity",
            limit=50,
            start_event_id=None,
        )

    @patch("zae_limiter.repository.Repository")
    def test_audit_list_with_start_event_id(self, mock_repo_class: Mock, runner: CliRunner) -> None:
        """Test audit list with pagination via start-event-id."""
        mock_repo = Mock()
        mock_repo.get_audit_events = AsyncMock(return_value=[])
        mock_repo.close = AsyncMock(return_value=None)
        mock_repo_class.return_value = mock_repo

        result = runner.invoke(
            cli, ["audit", "list", "-e", "test-entity", "--start-event-id", "01ABCDEF"]
        )

        assert result.exit_code == 0
        mock_repo.get_audit_events.assert_called_once_with(
            entity_id="test-entity",
            limit=100,
            start_event_id="01ABCDEF",
        )

    @patch("zae_limiter.repository.Repository")
    def test_audit_list_with_endpoint_url(self, mock_repo_class: Mock, runner: CliRunner) -> None:
        """Test audit list with --endpoint-url for LocalStack."""
        mock_repo = Mock()
        mock_repo.get_audit_events = AsyncMock(return_value=[])
        mock_repo.close = AsyncMock(return_value=None)
        mock_repo_class.return_value = mock_repo

        result = runner.invoke(
            cli,
            [
                "audit",
                "list",
                "-e",
                "test-entity",
                "--endpoint-url",
                "http://localhost:4566",
                "--region",
                "us-east-1",
            ],
        )

        assert result.exit_code == 0
        mock_repo_class.assert_called_once_with("limiter", "us-east-1", "http://localhost:4566")

    @patch("zae_limiter.repository.Repository")
    def test_audit_list_handles_exception(self, mock_repo_class: Mock, runner: CliRunner) -> None:
        """Test audit list handles exceptions gracefully."""
        mock_repo = Mock()
        mock_repo.get_audit_events = AsyncMock(side_effect=Exception("DynamoDB error"))
        mock_repo.close = AsyncMock(return_value=None)
        mock_repo_class.return_value = mock_repo

        result = runner.invoke(cli, ["audit", "list", "-e", "test-entity"])

        assert result.exit_code == 1
        assert "Error" in result.output
        assert "Failed to list audit events" in result.output

    @patch("zae_limiter.repository.Repository")
    def test_audit_list_handles_none_resource(
        self, mock_repo_class: Mock, runner: CliRunner
    ) -> None:
        """Test audit list handles None resource field."""
        from zae_limiter.models import AuditEvent

        mock_events = [
            AuditEvent(
                event_id="01ABCDEF",
                timestamp="2025-01-16T12:00:00Z",
                action="entity_deleted",
                entity_id="test-entity",
                principal=None,
                resource=None,
            ),
        ]

        mock_repo = Mock()
        mock_repo.get_audit_events = AsyncMock(return_value=mock_events)
        mock_repo.close = AsyncMock(return_value=None)
        mock_repo_class.return_value = mock_repo

        result = runner.invoke(cli, ["audit", "list", "-e", "test-entity"])

        assert result.exit_code == 0
        # None values should display as "-"
        assert "-" in result.output
        assert "entity_deleted" in result.output


class TestUsageCommands:
    """Test usage CLI commands."""

    def test_usage_help(self, runner: CliRunner) -> None:
        """Test usage command group help."""
        result = runner.invoke(cli, ["usage", "--help"])
        assert result.exit_code == 0
        assert "Usage snapshot commands" in result.output

    def test_usage_list_help(self, runner: CliRunner) -> None:
        """Test usage list command help."""
        result = runner.invoke(cli, ["usage", "list", "--help"])
        assert result.exit_code == 0
        assert "List usage snapshots" in result.output
        assert "--entity-id" in result.output
        assert "--resource" in result.output
        assert "--window" in result.output
        assert "--start" in result.output
        assert "--end" in result.output
        assert "--limit" in result.output

    def test_usage_list_requires_entity_or_resource(self, runner: CliRunner) -> None:
        """Test usage list requires --entity-id or --resource."""
        result = runner.invoke(cli, ["usage", "list"])
        assert result.exit_code != 0
        assert "entity-id" in result.output.lower() or "resource" in result.output.lower()

    def test_usage_summary_help(self, runner: CliRunner) -> None:
        """Test usage summary command help."""
        result = runner.invoke(cli, ["usage", "summary", "--help"])
        assert result.exit_code == 0
        assert "Show aggregated usage summary" in result.output

    def test_usage_summary_requires_entity_or_resource(self, runner: CliRunner) -> None:
        """Test usage summary requires --entity-id or --resource."""
        result = runner.invoke(cli, ["usage", "summary"])
        assert result.exit_code != 0
        assert "entity-id" in result.output.lower() or "resource" in result.output.lower()

    @patch("zae_limiter.repository.Repository")
    def test_usage_list_no_snapshots(self, mock_repo_class: Mock, runner: CliRunner) -> None:
        """Test usage list when no snapshots are found."""
        mock_repo = Mock()
        mock_repo.get_usage_snapshots = AsyncMock(return_value=([], None))
        mock_repo.close = AsyncMock(return_value=None)
        mock_repo_class.return_value = mock_repo

        result = runner.invoke(cli, ["usage", "list", "-e", "test-entity"])

        assert result.exit_code == 0
        assert "No usage snapshots found" in result.output

    @patch("zae_limiter.repository.Repository")
    def test_usage_list_with_snapshots(self, mock_repo_class: Mock, runner: CliRunner) -> None:
        """Test usage list displays snapshots in table format."""
        from zae_limiter.models import UsageSnapshot

        mock_snapshots = [
            UsageSnapshot(
                entity_id="test-entity",
                resource="gpt-4",
                window_start="2024-01-15T10:00:00Z",
                window_end="2024-01-15T10:59:59Z",
                window_type="hourly",
                counters={"tpm": 1000, "rpm": 5},
                total_events=5,
            ),
            UsageSnapshot(
                entity_id="test-entity",
                resource="gpt-4",
                window_start="2024-01-15T11:00:00Z",
                window_end="2024-01-15T11:59:59Z",
                window_type="hourly",
                counters={"tpm": 2000, "rpm": 10},
                total_events=10,
            ),
        ]

        mock_repo = Mock()
        mock_repo.get_usage_snapshots = AsyncMock(return_value=(mock_snapshots, None))
        mock_repo.close = AsyncMock(return_value=None)
        mock_repo_class.return_value = mock_repo

        result = runner.invoke(cli, ["usage", "list", "-e", "test-entity"])

        assert result.exit_code == 0
        assert "Usage Snapshots" in result.output
        assert "Window Start" in result.output
        assert "Resource" in result.output
        assert "gpt-4" in result.output
        assert "2024-01-15T10:00:00Z" in result.output
        assert "Total: 2 snapshots" in result.output

    @patch("zae_limiter.repository.Repository")
    def test_usage_list_by_resource(self, mock_repo_class: Mock, runner: CliRunner) -> None:
        """Test usage list by resource (GSI2 query)."""
        from zae_limiter.models import UsageSnapshot

        mock_snapshots = [
            UsageSnapshot(
                entity_id="entity-1",
                resource="gpt-4",
                window_start="2024-01-15T10:00:00Z",
                window_end="2024-01-15T10:59:59Z",
                window_type="hourly",
                counters={"tpm": 1000},
                total_events=5,
            ),
        ]

        mock_repo = Mock()
        mock_repo.get_usage_snapshots = AsyncMock(return_value=(mock_snapshots, None))
        mock_repo.close = AsyncMock(return_value=None)
        mock_repo_class.return_value = mock_repo

        result = runner.invoke(cli, ["usage", "list", "-r", "gpt-4"])

        assert result.exit_code == 0
        assert "gpt-4" in result.output
        mock_repo.get_usage_snapshots.assert_called_once()
        call_kwargs = mock_repo.get_usage_snapshots.call_args.kwargs
        assert call_kwargs["resource"] == "gpt-4"

    @patch("zae_limiter.repository.Repository")
    def test_usage_list_with_filters(self, mock_repo_class: Mock, runner: CliRunner) -> None:
        """Test usage list with window type and time filters."""
        mock_repo = Mock()
        mock_repo.get_usage_snapshots = AsyncMock(return_value=([], None))
        mock_repo.close = AsyncMock(return_value=None)
        mock_repo_class.return_value = mock_repo

        result = runner.invoke(
            cli,
            [
                "usage",
                "list",
                "-e",
                "test-entity",
                "-w",
                "hourly",
                "--start",
                "2024-01-15T00:00:00Z",
                "--end",
                "2024-01-15T23:59:59Z",
            ],
        )

        assert result.exit_code == 0
        mock_repo.get_usage_snapshots.assert_called_once()
        call_kwargs = mock_repo.get_usage_snapshots.call_args.kwargs
        assert call_kwargs["window_type"] == "hourly"
        assert call_kwargs["start_time"] == "2024-01-15T00:00:00Z"
        assert call_kwargs["end_time"] == "2024-01-15T23:59:59Z"

    @patch("zae_limiter.repository.Repository")
    def test_usage_list_shows_pagination_hint(
        self, mock_repo_class: Mock, runner: CliRunner
    ) -> None:
        """Test usage list shows pagination hint when more results available."""
        from zae_limiter.models import UsageSnapshot

        mock_snapshots = [
            UsageSnapshot(
                entity_id="test-entity",
                resource="gpt-4",
                window_start="2024-01-15T10:00:00Z",
                window_end="2024-01-15T10:59:59Z",
                window_type="hourly",
                counters={"tpm": 1000},
                total_events=5,
            ),
        ]
        # Return a next_key to indicate more results
        next_key = {"PK": {"S": "ENTITY#test"}, "SK": {"S": "#USAGE#..."}}

        mock_repo = Mock()
        mock_repo.get_usage_snapshots = AsyncMock(return_value=(mock_snapshots, next_key))
        mock_repo.close = AsyncMock(return_value=None)
        mock_repo_class.return_value = mock_repo

        result = runner.invoke(cli, ["usage", "list", "-e", "test-entity", "-l", "1"])

        assert result.exit_code == 0
        assert "more snapshots exist" in result.output.lower()

    @patch("zae_limiter.repository.Repository")
    def test_usage_summary_empty(self, mock_repo_class: Mock, runner: CliRunner) -> None:
        """Test usage summary with no data."""
        from zae_limiter.models import UsageSummary

        mock_summary = UsageSummary(
            snapshot_count=0,
            total={},
            average={},
            min_window_start=None,
            max_window_start=None,
        )

        mock_repo = Mock()
        mock_repo.get_usage_summary = AsyncMock(return_value=mock_summary)
        mock_repo.close = AsyncMock(return_value=None)
        mock_repo_class.return_value = mock_repo

        result = runner.invoke(cli, ["usage", "summary", "-e", "test-entity"])

        assert result.exit_code == 0
        assert "No usage data found" in result.output

    @patch("zae_limiter.repository.Repository")
    def test_usage_summary_with_data(self, mock_repo_class: Mock, runner: CliRunner) -> None:
        """Test usage summary displays aggregated data."""
        from zae_limiter.models import UsageSummary

        mock_summary = UsageSummary(
            snapshot_count=10,
            total={"tpm": 15000, "rpm": 75},
            average={"tpm": 1500.0, "rpm": 7.5},
            min_window_start="2024-01-15T10:00:00Z",
            max_window_start="2024-01-15T19:00:00Z",
        )

        mock_repo = Mock()
        mock_repo.get_usage_summary = AsyncMock(return_value=mock_summary)
        mock_repo.close = AsyncMock(return_value=None)
        mock_repo_class.return_value = mock_repo

        result = runner.invoke(cli, ["usage", "summary", "-e", "test-entity"])

        assert result.exit_code == 0
        assert "Usage Summary" in result.output
        assert "Snapshots:" in result.output
        assert "10" in result.output
        assert "Time Range:" in result.output
        assert "tpm" in result.output
        assert "rpm" in result.output
        assert "15000" in result.output or "15,000" in result.output

    @patch("zae_limiter.repository.Repository")
    def test_usage_summary_by_resource(self, mock_repo_class: Mock, runner: CliRunner) -> None:
        """Test usage summary by resource."""
        from zae_limiter.models import UsageSummary

        mock_summary = UsageSummary(
            snapshot_count=5,
            total={"tpm": 5000},
            average={"tpm": 1000.0},
            min_window_start="2024-01-15T10:00:00Z",
            max_window_start="2024-01-15T14:00:00Z",
        )

        mock_repo = Mock()
        mock_repo.get_usage_summary = AsyncMock(return_value=mock_summary)
        mock_repo.close = AsyncMock(return_value=None)
        mock_repo_class.return_value = mock_repo

        result = runner.invoke(cli, ["usage", "summary", "-r", "gpt-4"])

        assert result.exit_code == 0
        mock_repo.get_usage_summary.assert_called_once()
        call_kwargs = mock_repo.get_usage_summary.call_args.kwargs
        assert call_kwargs["resource"] == "gpt-4"

    def test_usage_list_invalid_name_with_underscore(self, runner: CliRunner) -> None:
        """Test usage list rejects names with underscores."""
        result = runner.invoke(cli, ["usage", "list", "--name", "rate_limits", "-e", "test"])
        assert result.exit_code != 0
        assert "underscore" in result.output.lower() or "hyphen" in result.output.lower()

    def test_usage_summary_invalid_name_with_underscore(self, runner: CliRunner) -> None:
        """Test usage summary rejects names with underscores."""
        result = runner.invoke(cli, ["usage", "summary", "--name", "rate_limits", "-e", "test"])
        assert result.exit_code != 0
        assert "underscore" in result.output.lower() or "hyphen" in result.output.lower()

    @patch("zae_limiter.repository.Repository")
    def test_usage_list_shows_long_entity_id_in_full(
        self, mock_repo_class: Mock, runner: CliRunner
    ) -> None:
        """Test usage list shows long entity IDs in full (auto-sized columns)."""
        from zae_limiter.models import UsageSnapshot

        long_entity_id = "very-long-entity-identifier-that-exceeds-display-width"
        mock_snapshots = [
            UsageSnapshot(
                entity_id=long_entity_id,
                resource="gpt-4",
                window_start="2024-01-15T10:00:00Z",
                window_end="2024-01-15T10:59:59Z",
                window_type="hourly",
                counters={"tpm": 1000},
                total_events=5,
            ),
        ]

        mock_repo = Mock()
        mock_repo.get_usage_snapshots = AsyncMock(return_value=(mock_snapshots, None))
        mock_repo.close = AsyncMock(return_value=None)
        mock_repo_class.return_value = mock_repo

        result = runner.invoke(cli, ["usage", "list", "-e", "long-entity"])

        assert result.exit_code == 0
        # Full entity shown in auto-sized table (TableRenderer)
        assert long_entity_id in result.output
        # Box-drawing table format
        assert "+-" in result.output

    @patch("zae_limiter.repository.Repository")
    def test_usage_list_shows_long_resource_name_in_full(
        self, mock_repo_class: Mock, runner: CliRunner
    ) -> None:
        """Test usage list shows long resource names in full (auto-sized columns)."""
        from zae_limiter.models import UsageSnapshot

        long_resource = "very-long-resource-name-that-exceeds-width"
        mock_snapshots = [
            UsageSnapshot(
                entity_id="user-123",
                resource=long_resource,
                window_start="2024-01-15T10:00:00Z",
                window_end="2024-01-15T10:59:59Z",
                window_type="hourly",
                counters={"tpm": 1000},
                total_events=5,
            ),
        ]

        mock_repo = Mock()
        mock_repo.get_usage_snapshots = AsyncMock(return_value=(mock_snapshots, None))
        mock_repo.close = AsyncMock(return_value=None)
        mock_repo_class.return_value = mock_repo

        result = runner.invoke(cli, ["usage", "list", "-e", "user-123"])

        assert result.exit_code == 0
        # Full resource shown in auto-sized table (TableRenderer)
        assert long_resource in result.output
        # Box-drawing table format
        assert "+-" in result.output

    @patch("zae_limiter.repository.Repository")
    def test_usage_list_value_error(self, mock_repo_class: Mock, runner: CliRunner) -> None:
        """Test usage list handles ValueError from repository."""
        mock_repo = Mock()
        mock_repo.get_usage_snapshots = AsyncMock(side_effect=ValueError("Invalid input"))
        mock_repo.close = AsyncMock(return_value=None)
        mock_repo_class.return_value = mock_repo

        result = runner.invoke(cli, ["usage", "list", "-e", "test-entity"])

        assert result.exit_code != 0
        assert "Invalid input" in result.output

    @patch("zae_limiter.repository.Repository")
    def test_usage_list_generic_exception(self, mock_repo_class: Mock, runner: CliRunner) -> None:
        """Test usage list handles generic exceptions from repository."""
        mock_repo = Mock()
        mock_repo.get_usage_snapshots = AsyncMock(side_effect=RuntimeError("Connection failed"))
        mock_repo.close = AsyncMock(return_value=None)
        mock_repo_class.return_value = mock_repo

        result = runner.invoke(cli, ["usage", "list", "-e", "test-entity"])

        assert result.exit_code != 0
        assert "Failed to list usage snapshots" in result.output

    @patch("zae_limiter.repository.Repository")
    def test_usage_summary_with_window_filter(
        self, mock_repo_class: Mock, runner: CliRunner
    ) -> None:
        """Test usage summary displays window filter info."""
        from zae_limiter.models import UsageSummary

        mock_summary = UsageSummary(
            snapshot_count=5,
            total={"tpm": 5000},
            average={"tpm": 1000.0},
            min_window_start="2024-01-15T10:00:00Z",
            max_window_start="2024-01-15T14:00:00Z",
        )

        mock_repo = Mock()
        mock_repo.get_usage_summary = AsyncMock(return_value=mock_summary)
        mock_repo.close = AsyncMock(return_value=None)
        mock_repo_class.return_value = mock_repo

        result = runner.invoke(cli, ["usage", "summary", "-e", "test-entity", "-w", "hourly"])

        assert result.exit_code == 0
        assert "Window:" in result.output
        assert "hourly" in result.output

    @patch("zae_limiter.repository.Repository")
    def test_usage_summary_value_error(self, mock_repo_class: Mock, runner: CliRunner) -> None:
        """Test usage summary handles ValueError from repository."""
        mock_repo = Mock()
        mock_repo.get_usage_summary = AsyncMock(side_effect=ValueError("Bad date format"))
        mock_repo.close = AsyncMock(return_value=None)
        mock_repo_class.return_value = mock_repo

        result = runner.invoke(cli, ["usage", "summary", "-e", "test-entity"])

        assert result.exit_code != 0
        assert "Bad date format" in result.output

    @patch("zae_limiter.repository.Repository")
    def test_usage_summary_generic_exception(
        self, mock_repo_class: Mock, runner: CliRunner
    ) -> None:
        """Test usage summary handles generic exceptions."""
        mock_repo = Mock()
        mock_repo.get_usage_summary = AsyncMock(side_effect=RuntimeError("Network timeout"))
        mock_repo.close = AsyncMock(return_value=None)
        mock_repo_class.return_value = mock_repo

        result = runner.invoke(cli, ["usage", "summary", "-e", "test-entity"])

        assert result.exit_code != 0
        assert "Failed to get usage summary" in result.output

    def test_usage_list_plot_help(self, runner: CliRunner) -> None:
        """Test usage list --plot option is in help."""
        result = runner.invoke(cli, ["usage", "list", "--help"])
        assert result.exit_code == 0
        assert "--plot" in result.output
        assert "ascii charts" in result.output.lower()

    @patch("zae_limiter.repository.Repository")
    def test_usage_list_with_plot(self, mock_repo_class: Mock, runner: CliRunner) -> None:
        """Test usage list with --plot flag generates ASCII charts."""
        from zae_limiter.models import UsageSnapshot

        mock_snapshots = [
            UsageSnapshot(
                entity_id="test-entity",
                resource="gpt-4",
                window_start="2024-01-15T11:00:00Z",
                window_end="2024-01-15T11:59:59Z",
                window_type="hourly",
                counters={"tpm": 2000, "rpm": 10},
                total_events=10,
            ),
            UsageSnapshot(
                entity_id="test-entity",
                resource="gpt-4",
                window_start="2024-01-15T10:00:00Z",
                window_end="2024-01-15T10:59:59Z",
                window_type="hourly",
                counters={"tpm": 1000, "rpm": 5},
                total_events=5,
            ),
        ]

        mock_repo = Mock()
        mock_repo.get_usage_snapshots = AsyncMock(return_value=(mock_snapshots, None))
        mock_repo.close = AsyncMock(return_value=None)
        mock_repo_class.return_value = mock_repo

        result = runner.invoke(cli, ["usage", "list", "-e", "test-entity", "--plot"])

        assert result.exit_code == 0
        # Plot output should have header with entity/resource context
        assert "Usage Plot: gpt-4 (hourly)" in result.output
        assert "Entity: test-entity" in result.output
        # Counter labels
        assert "TPM" in result.output
        assert "RPM" in result.output
        # Should have time range
        assert "Time range:" in result.output
        assert "Data points: 2" in result.output
        # Should still show total
        assert "Total: 2 snapshots" in result.output

    @patch("zae_limiter.repository.Repository")
    def test_usage_list_plot_shows_table_on_no_snapshots(
        self, mock_repo_class: Mock, runner: CliRunner
    ) -> None:
        """Test usage list --plot with no snapshots shows empty message."""
        mock_repo = Mock()
        mock_repo.get_usage_snapshots = AsyncMock(return_value=([], None))
        mock_repo.close = AsyncMock(return_value=None)
        mock_repo_class.return_value = mock_repo

        result = runner.invoke(cli, ["usage", "list", "-e", "test-entity", "--plot"])

        assert result.exit_code == 0
        assert "No usage snapshots found" in result.output

    @patch("zae_limiter.repository.Repository")
    @patch("zae_limiter.visualization.factory.PlotFormatter")
    def test_usage_list_plot_falls_back_to_table(
        self, mock_plot_formatter: Mock, mock_repo_class: Mock, runner: CliRunner
    ) -> None:
        """Test usage list --plot falls back to table if asciichartpy not installed."""
        from zae_limiter.models import UsageSnapshot

        mock_snapshots = [
            UsageSnapshot(
                entity_id="test-entity",
                resource="gpt-4",
                window_start="2024-01-15T10:00:00Z",
                window_end="2024-01-15T10:59:59Z",
                window_type="hourly",
                counters={"tpm": 1000},
                total_events=5,
            ),
        ]

        mock_repo = Mock()
        mock_repo.get_usage_snapshots = AsyncMock(return_value=(mock_snapshots, None))
        mock_repo.close = AsyncMock(return_value=None)
        mock_repo_class.return_value = mock_repo

        # Simulate asciichartpy not installed
        mock_plot_formatter.side_effect = ImportError(
            "asciichartpy is required for plot format. "
            "Install with: pip install 'zae-limiter[plot]'"
        )

        result = runner.invoke(cli, ["usage", "list", "-e", "test-entity", "--plot"])

        assert result.exit_code == 0
        # Should show warning and fallback message
        assert "Warning:" in result.output
        assert "Falling back to table format" in result.output
        # Should show table output
        assert "Usage Snapshots" in result.output
        assert "Window Start" in result.output


class TestListCommand:
    """Test list CLI command."""

    def test_list_help(self, runner: CliRunner) -> None:
        """Test list command help."""
        result = runner.invoke(cli, ["list", "--help"])
        assert result.exit_code == 0
        assert "List all deployed rate limiter instances" in result.output
        assert "--region" in result.output
        assert "--endpoint-url" in result.output

    @patch("zae_limiter.infra.discovery.InfrastructureDiscovery")
    def test_list_empty_result(self, mock_discovery_class: Mock, runner: CliRunner) -> None:
        """Test list command when no stacks exist."""
        mock_discovery = Mock()
        mock_discovery.list_limiters = AsyncMock(return_value=[])
        mock_discovery.__aenter__ = AsyncMock(return_value=mock_discovery)
        mock_discovery.__aexit__ = AsyncMock()
        mock_discovery_class.return_value = mock_discovery

        result = runner.invoke(cli, ["list"])

        assert result.exit_code == 0
        assert "No rate limiter instances found in region" in result.output
        assert "zae-limiter deploy --name my-app" in result.output

    @patch("zae_limiter.infra.discovery.InfrastructureDiscovery")
    def test_list_with_instances(self, mock_discovery_class: Mock, runner: CliRunner) -> None:
        """Test list command displays instances in table format."""
        from zae_limiter.models import LimiterInfo

        mock_limiters = [
            LimiterInfo(
                stack_name="my-app",
                user_name="my-app",
                region="us-east-1",
                stack_status="CREATE_COMPLETE",
                creation_time="2024-01-15T10:30:00Z",
                version="0.5.0",
                lambda_version="0.5.0",
                schema_version="1.0.0",
            ),
            LimiterInfo(
                stack_name="other-app",
                user_name="other-app",
                region="us-east-1",
                stack_status="UPDATE_COMPLETE",
                creation_time="2024-01-14T09:00:00Z",
                last_updated_time="2024-01-16T14:00:00Z",
                version="0.4.0",
            ),
        ]

        mock_discovery = Mock()
        mock_discovery.list_limiters = AsyncMock(return_value=mock_limiters)
        mock_discovery.__aenter__ = AsyncMock(return_value=mock_discovery)
        mock_discovery.__aexit__ = AsyncMock()
        mock_discovery_class.return_value = mock_discovery

        result = runner.invoke(cli, ["list"])

        assert result.exit_code == 0
        assert "Rate Limiter Instances" in result.output
        assert "my-app" in result.output
        assert "other-app" in result.output
        # Full status shown in rich table format
        assert "CREATE_COMPLETE" in result.output
        assert "UPDATE_COMPLETE" in result.output
        assert "0.5.0" in result.output
        assert "Total: 2 instance(s)" in result.output
        # Box-drawing table borders
        assert "+-" in result.output
        assert "| Name" in result.output

    @patch("zae_limiter.infra.discovery.InfrastructureDiscovery")
    def test_list_shows_healthy_status(self, mock_discovery_class: Mock, runner: CliRunner) -> None:
        """Test list command shows healthy status in table."""
        from zae_limiter.models import LimiterInfo

        mock_limiters = [
            LimiterInfo(
                stack_name="healthy",
                user_name="healthy",
                region="us-east-1",
                stack_status="CREATE_COMPLETE",
                creation_time="2024-01-15T10:30:00Z",
            ),
        ]

        mock_discovery = Mock()
        mock_discovery.list_limiters = AsyncMock(return_value=mock_limiters)
        mock_discovery.__aenter__ = AsyncMock(return_value=mock_discovery)
        mock_discovery.__aexit__ = AsyncMock()
        mock_discovery_class.return_value = mock_discovery

        result = runner.invoke(cli, ["list"])

        assert result.exit_code == 0
        assert "CREATE_COMPLETE" in result.output
        # No problem summary for healthy stacks
        assert "failed" not in result.output
        assert "in progress" not in result.output

    @patch("zae_limiter.infra.discovery.InfrastructureDiscovery")
    def test_list_shows_in_progress_summary(
        self, mock_discovery_class: Mock, runner: CliRunner
    ) -> None:
        """Test list command shows in-progress summary."""
        from zae_limiter.models import LimiterInfo

        mock_limiters = [
            LimiterInfo(
                stack_name="updating",
                user_name="updating",
                region="us-east-1",
                stack_status="UPDATE_IN_PROGRESS",
                creation_time="2024-01-15T10:30:00Z",
            ),
        ]

        mock_discovery = Mock()
        mock_discovery.list_limiters = AsyncMock(return_value=mock_limiters)
        mock_discovery.__aenter__ = AsyncMock(return_value=mock_discovery)
        mock_discovery.__aexit__ = AsyncMock()
        mock_discovery_class.return_value = mock_discovery

        result = runner.invoke(cli, ["list"])

        assert result.exit_code == 0
        assert "UPDATE_IN_PROGRESS" in result.output
        assert "1 in progress" in result.output

    @patch("zae_limiter.infra.discovery.InfrastructureDiscovery")
    def test_list_shows_failed_summary(self, mock_discovery_class: Mock, runner: CliRunner) -> None:
        """Test list command shows failed summary."""
        from zae_limiter.models import LimiterInfo

        mock_limiters = [
            LimiterInfo(
                stack_name="failed",
                user_name="failed",
                region="us-east-1",
                stack_status="CREATE_FAILED",
                creation_time="2024-01-15T10:30:00Z",
            ),
        ]

        mock_discovery = Mock()
        mock_discovery.list_limiters = AsyncMock(return_value=mock_limiters)
        mock_discovery.__aenter__ = AsyncMock(return_value=mock_discovery)
        mock_discovery.__aexit__ = AsyncMock()
        mock_discovery_class.return_value = mock_discovery

        result = runner.invoke(cli, ["list"])

        assert result.exit_code == 0
        assert "CREATE_FAILED" in result.output
        assert "1 failed" in result.output

    @patch("zae_limiter.infra.discovery.InfrastructureDiscovery")
    def test_list_shows_full_names(self, mock_discovery_class: Mock, runner: CliRunner) -> None:
        """Test list command shows full names for copy/paste usability."""
        from zae_limiter.models import LimiterInfo

        mock_limiters = [
            LimiterInfo(
                stack_name="very-long-name-exceeding-limit",
                user_name="very-long-name-exceeding-limit",
                region="us-east-1",
                stack_status="UPDATE_ROLLBACK_COMPLETE_CLEANUP_IN_PROGRESS",
                creation_time="2024-01-15T10:30:00Z",
                version="1.2.3-beta.4567890",
            ),
        ]

        mock_discovery = Mock()
        mock_discovery.list_limiters = AsyncMock(return_value=mock_limiters)
        mock_discovery.__aenter__ = AsyncMock(return_value=mock_discovery)
        mock_discovery.__aexit__ = AsyncMock()
        mock_discovery_class.return_value = mock_discovery

        result = runner.invoke(cli, ["list"])

        assert result.exit_code == 0
        # Full name shown in rich table format
        assert "very-long-name-exceeding-limit" in result.output
        # Full status shown
        assert "UPDATE_ROLLBACK_COMPLETE_CLEANUP_IN_PROGRESS" in result.output

    @patch("zae_limiter.infra.discovery.InfrastructureDiscovery")
    def test_list_with_region(self, mock_discovery_class: Mock, runner: CliRunner) -> None:
        """Test list command with --region option."""
        mock_discovery = Mock()
        mock_discovery.list_limiters = AsyncMock(return_value=[])
        mock_discovery.__aenter__ = AsyncMock(return_value=mock_discovery)
        mock_discovery.__aexit__ = AsyncMock()
        mock_discovery_class.return_value = mock_discovery

        result = runner.invoke(cli, ["list", "--region", "eu-west-1"])

        assert result.exit_code == 0
        mock_discovery_class.assert_called_once_with(region="eu-west-1", endpoint_url=None)

    @patch("zae_limiter.infra.discovery.InfrastructureDiscovery")
    def test_list_with_endpoint_url(self, mock_discovery_class: Mock, runner: CliRunner) -> None:
        """Test list command with --endpoint-url for LocalStack."""
        mock_discovery = Mock()
        mock_discovery.list_limiters = AsyncMock(return_value=[])
        mock_discovery.__aenter__ = AsyncMock(return_value=mock_discovery)
        mock_discovery.__aexit__ = AsyncMock()
        mock_discovery_class.return_value = mock_discovery

        result = runner.invoke(
            cli,
            ["list", "--endpoint-url", "http://localhost:4566", "--region", "us-east-1"],
        )

        assert result.exit_code == 0
        mock_discovery_class.assert_called_once_with(
            region="us-east-1", endpoint_url="http://localhost:4566"
        )

    @patch("zae_limiter.infra.discovery.InfrastructureDiscovery")
    def test_list_handles_exception(self, mock_discovery_class: Mock, runner: CliRunner) -> None:
        """Test list command handles exceptions gracefully."""
        mock_discovery = Mock()
        mock_discovery.list_limiters = AsyncMock(side_effect=Exception("CloudFormation API error"))
        mock_discovery.__aenter__ = AsyncMock(return_value=mock_discovery)
        mock_discovery.__aexit__ = AsyncMock()
        mock_discovery_class.return_value = mock_discovery

        result = runner.invoke(cli, ["list"])

        assert result.exit_code == 1
        assert "Failed to list limiters" in result.output

    @patch("zae_limiter.infra.discovery.InfrastructureDiscovery")
    def test_list_shows_na_for_missing_versions(
        self, mock_discovery_class: Mock, runner: CliRunner
    ) -> None:
        """Test list command shows N/A for missing version info."""
        from zae_limiter.models import LimiterInfo

        mock_limiters = [
            LimiterInfo(
                stack_name="no-tags",
                user_name="no-tags",
                region="us-east-1",
                stack_status="CREATE_COMPLETE",
                creation_time="2024-01-15T10:30:00Z",
                version=None,
                lambda_version=None,
                schema_version=None,
            ),
        ]

        mock_discovery = Mock()
        mock_discovery.list_limiters = AsyncMock(return_value=mock_limiters)
        mock_discovery.__aenter__ = AsyncMock(return_value=mock_discovery)
        mock_discovery.__aexit__ = AsyncMock()
        mock_discovery_class.return_value = mock_discovery

        result = runner.invoke(cli, ["list"])

        assert result.exit_code == 0
        # Missing versions shown as "-" for compact display
        assert "no-tags" in result.output

    @patch("zae_limiter.infra.discovery.InfrastructureDiscovery")
    def test_list_formats_creation_date(
        self, mock_discovery_class: Mock, runner: CliRunner
    ) -> None:
        """Test list command formats creation date correctly."""
        from zae_limiter.models import LimiterInfo

        mock_limiters = [
            LimiterInfo(
                stack_name="test",
                user_name="test",
                region="us-east-1",
                stack_status="CREATE_COMPLETE",
                creation_time="2024-01-15T10:30:00+00:00",
            ),
        ]

        mock_discovery = Mock()
        mock_discovery.list_limiters = AsyncMock(return_value=mock_limiters)
        mock_discovery.__aenter__ = AsyncMock(return_value=mock_discovery)
        mock_discovery.__aexit__ = AsyncMock()
        mock_discovery_class.return_value = mock_discovery

        result = runner.invoke(cli, ["list"])

        assert result.exit_code == 0
        assert "2024-01-15" in result.output

    @patch("zae_limiter.infra.discovery.InfrastructureDiscovery")
    def test_list_handles_invalid_creation_time(
        self, mock_discovery_class: Mock, runner: CliRunner
    ) -> None:
        """Test list command handles invalid creation time format."""
        from zae_limiter.models import LimiterInfo

        mock_limiters = [
            LimiterInfo(
                stack_name="test",
                user_name="test",
                region="us-east-1",
                stack_status="CREATE_COMPLETE",
                creation_time="invalid-date-format",
            ),
        ]

        mock_discovery = Mock()
        mock_discovery.list_limiters = AsyncMock(return_value=mock_limiters)
        mock_discovery.__aenter__ = AsyncMock(return_value=mock_discovery)
        mock_discovery.__aexit__ = AsyncMock()
        mock_discovery_class.return_value = mock_discovery

        result = runner.invoke(cli, ["list"])

        assert result.exit_code == 0
        assert "unknown" in result.output

    @patch("zae_limiter.infra.discovery.InfrastructureDiscovery")
    def test_list_shows_region_in_header(
        self, mock_discovery_class: Mock, runner: CliRunner
    ) -> None:
        """Test list command shows region in header."""
        from zae_limiter.models import LimiterInfo

        mock_limiters = [
            LimiterInfo(
                stack_name="test",
                user_name="test",
                region="ap-northeast-1",
                stack_status="CREATE_COMPLETE",
                creation_time="2024-01-15T10:30:00Z",
            ),
        ]

        mock_discovery = Mock()
        mock_discovery.list_limiters = AsyncMock(return_value=mock_limiters)
        mock_discovery.__aenter__ = AsyncMock(return_value=mock_discovery)
        mock_discovery.__aexit__ = AsyncMock()
        mock_discovery_class.return_value = mock_discovery

        result = runner.invoke(cli, ["list", "--region", "ap-northeast-1"])

        assert result.exit_code == 0
        assert "ap-northeast-1" in result.output

    @patch("zae_limiter.infra.discovery.InfrastructureDiscovery")
    def test_list_shows_default_region_when_not_specified(
        self, mock_discovery_class: Mock, runner: CliRunner
    ) -> None:
        """Test list command shows 'default' when region not specified."""
        mock_discovery = Mock()
        mock_discovery.list_limiters = AsyncMock(return_value=[])
        mock_discovery.__aenter__ = AsyncMock(return_value=mock_discovery)
        mock_discovery.__aexit__ = AsyncMock()
        mock_discovery_class.return_value = mock_discovery

        result = runner.invoke(cli, ["list"])

        assert result.exit_code == 0
        assert "default" in result.output

    @patch("zae_limiter.infra.discovery.InfrastructureDiscovery")
    def test_list_counts_problems(self, mock_discovery_class: Mock, runner: CliRunner) -> None:
        """Test list command counts failed and in-progress stacks."""
        from zae_limiter.models import LimiterInfo

        mock_limiters = [
            LimiterInfo(
                stack_name="healthy",
                user_name="healthy",
                region="us-east-1",
                stack_status="CREATE_COMPLETE",
                creation_time="2024-01-15T10:30:00Z",
            ),
            LimiterInfo(
                stack_name="failed",
                user_name="failed",
                region="us-east-1",
                stack_status="CREATE_FAILED",
                creation_time="2024-01-15T10:30:00Z",
            ),
            LimiterInfo(
                stack_name="updating",
                user_name="updating",
                region="us-east-1",
                stack_status="UPDATE_IN_PROGRESS",
                creation_time="2024-01-15T10:30:00Z",
            ),
        ]

        mock_discovery = Mock()
        mock_discovery.list_limiters = AsyncMock(return_value=mock_limiters)
        mock_discovery.__aenter__ = AsyncMock(return_value=mock_discovery)
        mock_discovery.__aexit__ = AsyncMock()
        mock_discovery_class.return_value = mock_discovery

        result = runner.invoke(cli, ["list"])

        assert result.exit_code == 0
        assert "Total: 3 instance(s)" in result.output
        # New format shows separate counts
        assert "1 failed" in result.output
        assert "1 in progress" in result.output

    @patch("zae_limiter.infra.discovery.InfrastructureDiscovery")
    def test_list_handles_unknown_status(
        self, mock_discovery_class: Mock, runner: CliRunner
    ) -> None:
        """Test list command handles unknown status gracefully."""
        from zae_limiter.models import LimiterInfo

        mock_limiters = [
            LimiterInfo(
                stack_name="unknown",
                user_name="unknown",
                region="us-east-1",
                stack_status="IMPORT_COMPLETE",  # Not healthy, not in_progress, not failed
                creation_time="2024-01-15T10:30:00Z",
            ),
        ]

        mock_discovery = Mock()
        mock_discovery.list_limiters = AsyncMock(return_value=mock_limiters)
        mock_discovery.__aenter__ = AsyncMock(return_value=mock_discovery)
        mock_discovery.__aexit__ = AsyncMock()
        mock_discovery_class.return_value = mock_discovery

        result = runner.invoke(cli, ["list"])

        assert result.exit_code == 0
        # Should not show problem count for IMPORT_COMPLETE
        assert "instance(s) need attention" not in result.output


class TestResourceCommands:
    """Test resource CLI commands."""

    def test_resource_help(self, runner: CliRunner) -> None:
        """Test resource command group help."""
        result = runner.invoke(cli, ["resource", "--help"])
        assert result.exit_code == 0
        assert "Resource-level default limit configuration" in result.output

    def test_resource_set_help(self, runner: CliRunner) -> None:
        """Test resource set-defaults command help."""
        result = runner.invoke(cli, ["resource", "set-defaults", "--help"])
        assert result.exit_code == 0
        assert "Set default limits for a resource" in result.output
        assert "--limit" in result.output
        assert "RESOURCE_NAME" in result.output

    def test_resource_get_help(self, runner: CliRunner) -> None:
        """Test resource get-defaults command help."""
        result = runner.invoke(cli, ["resource", "get-defaults", "--help"])
        assert result.exit_code == 0
        assert "Get default limits for a resource" in result.output
        assert "RESOURCE_NAME" in result.output

    def test_resource_delete_help(self, runner: CliRunner) -> None:
        """Test resource delete-defaults command help."""
        result = runner.invoke(cli, ["resource", "delete-defaults", "--help"])
        assert result.exit_code == 0
        assert "Delete default limits for a resource" in result.output
        assert "--yes" in result.output

    def test_resource_list_help(self, runner: CliRunner) -> None:
        """Test resource list command help."""
        result = runner.invoke(cli, ["resource", "list", "--help"])
        assert result.exit_code == 0
        assert "List all resources with configured defaults" in result.output

    def test_resource_set_requires_resource_name(self, runner: CliRunner) -> None:
        """Test resource set-defaults requires RESOURCE_NAME argument."""
        result = runner.invoke(cli, ["resource", "set-defaults", "-l", "tpm:10000"])
        assert result.exit_code != 0
        assert "RESOURCE_NAME" in result.output

    def test_resource_set_requires_limit(self, runner: CliRunner) -> None:
        """Test resource set-defaults requires at least one --limit."""
        result = runner.invoke(cli, ["resource", "set-defaults", "gpt-4"])
        assert result.exit_code != 0
        assert "limit" in result.output.lower()

    @patch("zae_limiter.repository.Repository")
    def test_resource_set_success(self, mock_repo_class: Mock, runner: CliRunner) -> None:
        """Test resource set-defaults with valid limits."""
        mock_repo = Mock()
        mock_repo.set_resource_defaults = AsyncMock(return_value=None)
        mock_repo.close = AsyncMock(return_value=None)
        mock_repo_class.return_value = mock_repo

        result = runner.invoke(
            cli, ["resource", "set-defaults", "gpt-4", "-l", "tpm:10000", "-l", "rpm:500:1000"]
        )

        assert result.exit_code == 0
        assert "Set 2 default(s) for resource 'gpt-4'" in result.output
        assert "tpm: 10,000/min (burst: 10,000)" in result.output
        assert "rpm: 500/min (burst: 1,000)" in result.output

        # Verify repository was called correctly
        mock_repo.set_resource_defaults.assert_called_once()
        call_args = mock_repo.set_resource_defaults.call_args
        assert call_args[0][0] == "gpt-4"
        limits = call_args[0][1]
        assert len(limits) == 2
        assert limits[0].name == "tpm"
        assert limits[0].capacity == 10000
        assert limits[1].name == "rpm"
        assert limits[1].capacity == 500
        assert limits[1].burst == 1000

    def test_resource_set_invalid_limit_format(self, runner: CliRunner) -> None:
        """Test resource set-defaults with invalid limit format."""
        result = runner.invoke(cli, ["resource", "set-defaults", "gpt-4", "-l", "invalid"])

        assert result.exit_code == 1
        assert "Invalid limit format" in result.output

    def test_resource_set_invalid_limit_values(self, runner: CliRunner) -> None:
        """Test resource set-defaults with non-numeric limit values."""
        result = runner.invoke(cli, ["resource", "set-defaults", "gpt-4", "-l", "tpm:abc"])

        assert result.exit_code == 1
        assert "Invalid limit values" in result.output

    @patch("zae_limiter.repository.Repository")
    def test_resource_set_validation_error(self, mock_repo_class: Mock, runner: CliRunner) -> None:
        """Test resource set-defaults handles ValidationError."""
        from zae_limiter.exceptions import ValidationError

        mock_repo = Mock()
        mock_repo.set_resource_defaults = AsyncMock(
            side_effect=ValidationError(
                field="resource", value="gpt-4", reason="Invalid resource name"
            )
        )
        mock_repo.close = AsyncMock(return_value=None)
        mock_repo_class.return_value = mock_repo

        result = runner.invoke(cli, ["resource", "set-defaults", "gpt-4", "-l", "tpm:10000"])

        assert result.exit_code == 1
        assert "Invalid resource name" in result.output

    @patch("zae_limiter.repository.Repository")
    def test_resource_get_with_limits(self, mock_repo_class: Mock, runner: CliRunner) -> None:
        """Test resource get-defaults displays limits."""
        from zae_limiter.models import Limit

        mock_limits = [
            Limit(
                name="tpm",
                capacity=10000,
                burst=10000,
                refill_amount=10000,
                refill_period_seconds=60,
            ),
            Limit(
                name="rpm",
                capacity=500,
                burst=1000,
                refill_amount=500,
                refill_period_seconds=60,
            ),
        ]

        mock_repo = Mock()
        mock_repo.get_resource_defaults = AsyncMock(return_value=mock_limits)
        mock_repo.close = AsyncMock(return_value=None)
        mock_repo_class.return_value = mock_repo

        result = runner.invoke(cli, ["resource", "get-defaults", "gpt-4"])

        assert result.exit_code == 0
        assert "Defaults for resource 'gpt-4'" in result.output
        assert "tpm: 10,000/min (burst: 10,000)" in result.output
        assert "rpm: 500/min (burst: 1,000)" in result.output

    @patch("zae_limiter.repository.Repository")
    def test_resource_get_no_limits(self, mock_repo_class: Mock, runner: CliRunner) -> None:
        """Test resource get-defaults when no limits configured."""
        mock_repo = Mock()
        mock_repo.get_resource_defaults = AsyncMock(return_value=[])
        mock_repo.close = AsyncMock(return_value=None)
        mock_repo_class.return_value = mock_repo

        result = runner.invoke(cli, ["resource", "get-defaults", "gpt-4"])

        assert result.exit_code == 0
        assert "No defaults configured for resource 'gpt-4'" in result.output

    @patch("zae_limiter.repository.Repository")
    def test_resource_delete_with_confirmation(
        self, mock_repo_class: Mock, runner: CliRunner
    ) -> None:
        """Test resource delete-defaults with user confirmation."""
        mock_repo = Mock()
        mock_repo.delete_resource_defaults = AsyncMock(return_value=None)
        mock_repo.close = AsyncMock(return_value=None)
        mock_repo_class.return_value = mock_repo

        result = runner.invoke(cli, ["resource", "delete-defaults", "gpt-4"], input="y\n")

        assert result.exit_code == 0
        assert "Deleted defaults for resource 'gpt-4'" in result.output

    @patch("zae_limiter.repository.Repository")
    def test_resource_delete_cancelled(self, mock_repo_class: Mock, runner: CliRunner) -> None:
        """Test resource delete-defaults cancelled by user."""
        result = runner.invoke(cli, ["resource", "delete-defaults", "gpt-4"], input="n\n")

        assert result.exit_code == 0
        assert "Cancelled" in result.output

    @patch("zae_limiter.repository.Repository")
    def test_resource_delete_with_yes_flag(self, mock_repo_class: Mock, runner: CliRunner) -> None:
        """Test resource delete-defaults with --yes flag skips confirmation."""
        mock_repo = Mock()
        mock_repo.delete_resource_defaults = AsyncMock(return_value=None)
        mock_repo.close = AsyncMock(return_value=None)
        mock_repo_class.return_value = mock_repo

        result = runner.invoke(cli, ["resource", "delete-defaults", "gpt-4", "--yes"])

        assert result.exit_code == 0
        assert "Deleted defaults for resource 'gpt-4'" in result.output

    @patch("zae_limiter.repository.Repository")
    def test_resource_list_with_resources(self, mock_repo_class: Mock, runner: CliRunner) -> None:
        """Test resource list displays resources."""
        mock_repo = Mock()
        mock_repo.list_resources_with_defaults = AsyncMock(
            return_value=["claude-3", "gpt-4", "llama-70b"]
        )
        mock_repo.close = AsyncMock(return_value=None)
        mock_repo_class.return_value = mock_repo

        result = runner.invoke(cli, ["resource", "list"])

        assert result.exit_code == 0
        assert "Resources with configured defaults:" in result.output
        assert "claude-3" in result.output
        assert "gpt-4" in result.output
        assert "llama-70b" in result.output

    @patch("zae_limiter.repository.Repository")
    def test_resource_list_no_resources(self, mock_repo_class: Mock, runner: CliRunner) -> None:
        """Test resource list when no resources configured."""
        mock_repo = Mock()
        mock_repo.list_resources_with_defaults = AsyncMock(return_value=[])
        mock_repo.close = AsyncMock(return_value=None)
        mock_repo_class.return_value = mock_repo

        result = runner.invoke(cli, ["resource", "list"])

        assert result.exit_code == 0
        assert "No resources with configured defaults" in result.output

    @patch("zae_limiter.repository.Repository")
    def test_resource_set_handles_exception(self, mock_repo_class: Mock, runner: CliRunner) -> None:
        """Test resource set-defaults handles unexpected exceptions."""
        mock_repo = Mock()
        mock_repo.set_resource_defaults = AsyncMock(side_effect=Exception("DynamoDB error"))
        mock_repo.close = AsyncMock(return_value=None)
        mock_repo_class.return_value = mock_repo

        result = runner.invoke(cli, ["resource", "set-defaults", "gpt-4", "-l", "tpm:10000"])

        assert result.exit_code == 1
        assert "Failed to set resource defaults" in result.output

    @patch("zae_limiter.repository.Repository")
    def test_resource_get_handles_exception(self, mock_repo_class: Mock, runner: CliRunner) -> None:
        """Test resource get-defaults handles unexpected exceptions."""
        mock_repo = Mock()
        mock_repo.get_resource_defaults = AsyncMock(side_effect=Exception("Connection failed"))
        mock_repo.close = AsyncMock(return_value=None)
        mock_repo_class.return_value = mock_repo

        result = runner.invoke(cli, ["resource", "get-defaults", "gpt-4"])

        assert result.exit_code == 1
        assert "Failed to get resource defaults" in result.output


class TestSystemCommands:
    """Test system CLI commands."""

    def test_system_help(self, runner: CliRunner) -> None:
        """Test system command group help."""
        result = runner.invoke(cli, ["system", "--help"])
        assert result.exit_code == 0
        assert "System-level default limit configuration" in result.output

    def test_system_set_defaults_help(self, runner: CliRunner) -> None:
        """Test system set-defaults command help."""
        result = runner.invoke(cli, ["system", "set-defaults", "--help"])
        assert result.exit_code == 0
        assert "Set system-wide default limits" in result.output
        assert "--limit" in result.output

    def test_system_get_defaults_help(self, runner: CliRunner) -> None:
        """Test system get-defaults command help."""
        result = runner.invoke(cli, ["system", "get-defaults", "--help"])
        assert result.exit_code == 0
        assert "Get system-wide default limits" in result.output

    def test_system_delete_defaults_help(self, runner: CliRunner) -> None:
        """Test system delete-defaults command help."""
        result = runner.invoke(cli, ["system", "delete-defaults", "--help"])
        assert result.exit_code == 0
        assert "Delete" in result.output
        assert "--yes" in result.output

    def test_system_set_defaults_requires_limit(self, runner: CliRunner) -> None:
        """Test system set-defaults requires at least one --limit."""
        result = runner.invoke(cli, ["system", "set-defaults"])
        assert result.exit_code != 0
        assert "limit" in result.output.lower()

    @patch("zae_limiter.repository.Repository")
    def test_system_set_defaults_success(self, mock_repo_class: Mock, runner: CliRunner) -> None:
        """Test system set-defaults with valid limits."""
        mock_repo = Mock()
        mock_repo.set_system_defaults = AsyncMock(return_value=None)
        mock_repo.close = AsyncMock(return_value=None)
        mock_repo_class.return_value = mock_repo

        result = runner.invoke(
            cli,
            ["system", "set-defaults", "-l", "tpm:10000", "-l", "rpm:500:1000"],
        )

        assert result.exit_code == 0
        assert "Set 2 system-wide default(s)" in result.output
        assert "tpm: 10,000/min (burst: 10,000)" in result.output
        assert "rpm: 500/min (burst: 1,000)" in result.output

        # Verify repository was called correctly
        mock_repo.set_system_defaults.assert_called_once()
        call_args = mock_repo.set_system_defaults.call_args
        limits = call_args[0][0]  # First positional arg is limits
        assert len(limits) == 2

    @patch("zae_limiter.repository.Repository")
    def test_system_set_defaults_with_on_unavailable(
        self, mock_repo_class: Mock, runner: CliRunner
    ) -> None:
        """Test system set-defaults with --on-unavailable option."""
        mock_repo = Mock()
        mock_repo.set_system_defaults = AsyncMock(return_value=None)
        mock_repo.close = AsyncMock(return_value=None)
        mock_repo_class.return_value = mock_repo

        result = runner.invoke(
            cli,
            ["system", "set-defaults", "-l", "tpm:10000", "--on-unavailable", "allow"],
        )

        assert result.exit_code == 0
        assert "Set 1 system-wide default(s)" in result.output
        assert "on_unavailable: allow" in result.output

        # Verify on_unavailable was passed
        mock_repo.set_system_defaults.assert_called_once()
        call_args = mock_repo.set_system_defaults.call_args
        assert call_args.kwargs.get("on_unavailable") == "allow"

    def test_system_set_defaults_invalid_limit_format(self, runner: CliRunner) -> None:
        """Test system set-defaults with invalid limit format."""
        result = runner.invoke(cli, ["system", "set-defaults", "-l", "invalid"])

        assert result.exit_code == 1
        assert "Invalid limit format" in result.output

    def test_system_set_defaults_invalid_limit_values(self, runner: CliRunner) -> None:
        """Test system set-defaults with non-numeric limit values."""
        result = runner.invoke(cli, ["system", "set-defaults", "-l", "tpm:abc"])

        assert result.exit_code == 1
        assert "Invalid limit values" in result.output

    @patch("zae_limiter.repository.Repository")
    def test_system_set_defaults_validation_error(
        self, mock_repo_class: Mock, runner: CliRunner
    ) -> None:
        """Test system set-defaults handles ValidationError."""
        from zae_limiter.exceptions import ValidationError

        mock_repo = Mock()
        mock_repo.set_system_defaults = AsyncMock(
            side_effect=ValidationError(field="limits", value="[]", reason="At least one limit")
        )
        mock_repo.close = AsyncMock(return_value=None)
        mock_repo_class.return_value = mock_repo

        result = runner.invoke(cli, ["system", "set-defaults", "-l", "tpm:10000"])

        assert result.exit_code == 1
        assert "At least one limit" in result.output

    @patch("zae_limiter.repository.Repository")
    def test_system_get_defaults_with_limits(
        self, mock_repo_class: Mock, runner: CliRunner
    ) -> None:
        """Test system get-defaults displays limits."""
        from zae_limiter.models import Limit

        mock_limits = [
            Limit(
                name="tpm",
                capacity=10000,
                burst=10000,
                refill_amount=10000,
                refill_period_seconds=60,
            ),
            Limit(
                name="rpm",
                capacity=500,
                burst=1000,
                refill_amount=500,
                refill_period_seconds=60,
            ),
        ]

        mock_repo = Mock()
        # get_system_defaults returns (limits, on_unavailable)
        mock_repo.get_system_defaults = AsyncMock(return_value=(mock_limits, "allow"))
        mock_repo.close = AsyncMock(return_value=None)
        mock_repo_class.return_value = mock_repo

        result = runner.invoke(cli, ["system", "get-defaults"])

        assert result.exit_code == 0
        assert "System-wide defaults" in result.output
        assert "tpm: 10,000/min (burst: 10,000)" in result.output
        assert "rpm: 500/min (burst: 1,000)" in result.output
        assert "on_unavailable: allow" in result.output

    @patch("zae_limiter.repository.Repository")
    def test_system_get_defaults_no_limits(self, mock_repo_class: Mock, runner: CliRunner) -> None:
        """Test system get-defaults when no limits configured."""
        mock_repo = Mock()
        # get_system_defaults returns (limits, on_unavailable)
        mock_repo.get_system_defaults = AsyncMock(return_value=([], None))
        mock_repo.close = AsyncMock(return_value=None)
        mock_repo_class.return_value = mock_repo

        result = runner.invoke(cli, ["system", "get-defaults"])

        assert result.exit_code == 0
        assert "No system defaults configured" in result.output

    @patch("zae_limiter.repository.Repository")
    def test_system_delete_defaults_with_confirmation(
        self, mock_repo_class: Mock, runner: CliRunner
    ) -> None:
        """Test system delete-defaults with user confirmation."""
        mock_repo = Mock()
        mock_repo.delete_system_defaults = AsyncMock(return_value=None)
        mock_repo.close = AsyncMock(return_value=None)
        mock_repo_class.return_value = mock_repo

        result = runner.invoke(cli, ["system", "delete-defaults"], input="y\n")

        assert result.exit_code == 0
        assert "Deleted all system-wide defaults" in result.output

    @patch("zae_limiter.repository.Repository")
    def test_system_delete_defaults_cancelled(
        self, mock_repo_class: Mock, runner: CliRunner
    ) -> None:
        """Test system delete-defaults cancelled by user."""
        result = runner.invoke(cli, ["system", "delete-defaults"], input="n\n")

        assert result.exit_code == 0
        assert "Cancelled" in result.output

    @patch("zae_limiter.repository.Repository")
    def test_system_delete_defaults_with_yes_flag(
        self, mock_repo_class: Mock, runner: CliRunner
    ) -> None:
        """Test system delete-defaults with --yes flag skips confirmation."""
        mock_repo = Mock()
        mock_repo.delete_system_defaults = AsyncMock(return_value=None)
        mock_repo.close = AsyncMock(return_value=None)
        mock_repo_class.return_value = mock_repo

        result = runner.invoke(cli, ["system", "delete-defaults", "--yes"])

        assert result.exit_code == 0
        assert "Deleted all system-wide defaults" in result.output

    @patch("zae_limiter.repository.Repository")
    def test_system_get_defaults_handles_exception(
        self, mock_repo_class: Mock, runner: CliRunner
    ) -> None:
        """Test system get-defaults handles unexpected exceptions."""
        mock_repo = Mock()
        mock_repo.get_system_defaults = AsyncMock(side_effect=Exception("Connection failed"))
        mock_repo.close = AsyncMock(return_value=None)
        mock_repo_class.return_value = mock_repo

        result = runner.invoke(cli, ["system", "get-defaults"])

        assert result.exit_code == 1
        assert "Failed to get system defaults" in result.output

    @patch("zae_limiter.repository.Repository")
    def test_system_set_defaults_handles_exception(
        self, mock_repo_class: Mock, runner: CliRunner
    ) -> None:
        """Test system set-defaults handles unexpected exceptions."""
        mock_repo = Mock()
        mock_repo.set_system_defaults = AsyncMock(side_effect=Exception("DynamoDB error"))
        mock_repo.close = AsyncMock(return_value=None)
        mock_repo_class.return_value = mock_repo

        result = runner.invoke(cli, ["system", "set-defaults", "-l", "tpm:10000"])

        assert result.exit_code == 1
        assert "Failed to set system defaults" in result.output

    @patch("zae_limiter.repository.Repository")
    def test_system_delete_defaults_handles_exception(
        self, mock_repo_class: Mock, runner: CliRunner
    ) -> None:
        """Test system delete-defaults handles unexpected exceptions."""
        mock_repo = Mock()
        mock_repo.delete_system_defaults = AsyncMock(side_effect=Exception("DynamoDB error"))
        mock_repo.close = AsyncMock(return_value=None)
        mock_repo_class.return_value = mock_repo

        result = runner.invoke(cli, ["system", "delete-defaults", "--yes"])

        assert result.exit_code == 1
        assert "Failed to delete system defaults" in result.output


class TestLimitParsing:
    """Test limit parsing and formatting helpers."""

    def test_parse_limit_name_and_capacity(self) -> None:
        """Test parsing limit with name:capacity format."""
        from zae_limiter.cli import _parse_limit

        limit = _parse_limit("tpm:10000")

        assert limit.name == "tpm"
        assert limit.capacity == 10000
        assert limit.burst == 10000  # Default to capacity
        assert limit.refill_amount == 10000
        assert limit.refill_period_seconds == 60

    def test_parse_limit_with_burst(self) -> None:
        """Test parsing limit with name:capacity:burst format."""
        from zae_limiter.cli import _parse_limit

        limit = _parse_limit("rpm:500:1000")

        assert limit.name == "rpm"
        assert limit.capacity == 500
        assert limit.burst == 1000
        assert limit.refill_amount == 500
        assert limit.refill_period_seconds == 60

    def test_parse_limit_invalid_format(self) -> None:
        """Test parsing limit with invalid format raises error."""
        import click

        from zae_limiter.cli import _parse_limit

        with pytest.raises(click.BadParameter) as exc_info:
            _parse_limit("invalid")

        assert "Invalid limit format" in str(exc_info.value)

    def test_parse_limit_invalid_capacity(self) -> None:
        """Test parsing limit with non-numeric capacity raises error."""
        import click

        from zae_limiter.cli import _parse_limit

        with pytest.raises(click.BadParameter) as exc_info:
            _parse_limit("tpm:abc")

        assert "Invalid limit values" in str(exc_info.value)

    def test_parse_limit_invalid_burst(self) -> None:
        """Test parsing limit with non-numeric burst raises error."""
        import click

        from zae_limiter.cli import _parse_limit

        with pytest.raises(click.BadParameter) as exc_info:
            _parse_limit("tpm:100:abc")

        assert "Invalid limit values" in str(exc_info.value)

    def test_format_limit(self) -> None:
        """Test formatting a limit for display."""
        from zae_limiter.cli import _format_limit
        from zae_limiter.models import Limit

        limit = Limit(
            name="tpm",
            capacity=10000,
            burst=15000,
            refill_amount=10000,
            refill_period_seconds=60,
        )

        formatted = _format_limit(limit)

        assert formatted == "tpm: 10,000/min (burst: 15,000)"

    def test_format_limit_same_burst(self) -> None:
        """Test formatting a limit where burst equals capacity."""
        from zae_limiter.cli import _format_limit
        from zae_limiter.models import Limit

        limit = Limit(
            name="rpm",
            capacity=500,
            burst=500,
            refill_amount=500,
            refill_period_seconds=60,
        )

        formatted = _format_limit(limit)

        assert formatted == "rpm: 500/min (burst: 500)"

    def test_format_limit_large_numbers(self) -> None:
        """Test formatting a limit with large numbers includes commas."""
        from zae_limiter.cli import _format_limit
        from zae_limiter.models import Limit

        limit = Limit(
            name="tpm",
            capacity=1000000,
            burst=2000000,
            refill_amount=1000000,
            refill_period_seconds=60,
        )

        formatted = _format_limit(limit)

        assert formatted == "tpm: 1,000,000/min (burst: 2,000,000)"


class TestResourceCommandsEdgeCases:
    """Test edge cases for resource CLI commands."""

    @patch("zae_limiter.repository.Repository")
    def test_resource_set_with_custom_name(self, mock_repo_class: Mock, runner: CliRunner) -> None:
        """Test resource set-defaults with custom --name option."""
        mock_repo = Mock()
        mock_repo.set_resource_defaults = AsyncMock(return_value=None)
        mock_repo.close = AsyncMock(return_value=None)
        mock_repo_class.return_value = mock_repo

        result = runner.invoke(
            cli, ["resource", "set-defaults", "gpt-4", "-n", "my-limiter", "-l", "tpm:10000"]
        )

        assert result.exit_code == 0
        # Verify Repository was instantiated with the custom name
        mock_repo_class.assert_called_once()
        call_args = mock_repo_class.call_args
        assert call_args[0][0] == "my-limiter"

    @patch("zae_limiter.repository.Repository")
    def test_resource_set_with_region(self, mock_repo_class: Mock, runner: CliRunner) -> None:
        """Test resource set-defaults with --region option."""
        mock_repo = Mock()
        mock_repo.set_resource_defaults = AsyncMock(return_value=None)
        mock_repo.close = AsyncMock(return_value=None)
        mock_repo_class.return_value = mock_repo

        result = runner.invoke(
            cli,
            ["resource", "set-defaults", "gpt-4", "--region", "eu-west-1", "-l", "tpm:10000"],
        )

        assert result.exit_code == 0
        mock_repo_class.assert_called_once()
        call_args = mock_repo_class.call_args
        assert call_args[0][1] == "eu-west-1"

    @patch("zae_limiter.repository.Repository")
    def test_resource_set_with_endpoint_url(self, mock_repo_class: Mock, runner: CliRunner) -> None:
        """Test resource set-defaults with --endpoint-url option."""
        mock_repo = Mock()
        mock_repo.set_resource_defaults = AsyncMock(return_value=None)
        mock_repo.close = AsyncMock(return_value=None)
        mock_repo_class.return_value = mock_repo

        result = runner.invoke(
            cli,
            [
                "resource",
                "set-defaults",
                "gpt-4",
                "--endpoint-url",
                "http://localhost:4566",
                "-l",
                "tpm:10000",
            ],
        )

        assert result.exit_code == 0
        mock_repo_class.assert_called_once()
        call_args = mock_repo_class.call_args
        assert call_args[0][2] == "http://localhost:4566"

    @patch("zae_limiter.repository.Repository")
    def test_resource_delete_handles_exception(
        self, mock_repo_class: Mock, runner: CliRunner
    ) -> None:
        """Test resource delete-defaults handles unexpected exceptions."""
        mock_repo = Mock()
        mock_repo.delete_resource_defaults = AsyncMock(side_effect=Exception("DynamoDB error"))
        mock_repo.close = AsyncMock(return_value=None)
        mock_repo_class.return_value = mock_repo

        result = runner.invoke(cli, ["resource", "delete-defaults", "gpt-4", "--yes"])

        assert result.exit_code == 1
        assert "Failed to delete resource defaults" in result.output

    @patch("zae_limiter.repository.Repository")
    def test_resource_list_handles_exception(
        self, mock_repo_class: Mock, runner: CliRunner
    ) -> None:
        """Test resource list handles unexpected exceptions."""
        mock_repo = Mock()
        mock_repo.list_resources_with_defaults = AsyncMock(side_effect=Exception("Scan failed"))
        mock_repo.close = AsyncMock(return_value=None)
        mock_repo_class.return_value = mock_repo

        result = runner.invoke(cli, ["resource", "list"])

        assert result.exit_code == 1
        assert "Failed to list resources" in result.output

    def test_resource_set_repo_init_validation_error(self, runner: CliRunner) -> None:
        """Test resource set-defaults handles ValidationError during Repository init."""
        from zae_limiter.exceptions import ValidationError

        with patch("zae_limiter.repository.Repository") as mock_repo_class:
            mock_repo_class.side_effect = ValidationError(
                field="name", value="invalid", reason="Invalid name format"
            )

            result = runner.invoke(cli, ["resource", "set-defaults", "gpt-4", "-l", "tpm:10000"])

            assert result.exit_code == 1
            assert "Invalid name format" in result.output

    def test_resource_get_repo_init_validation_error(self, runner: CliRunner) -> None:
        """Test resource get-defaults handles ValidationError during Repository init."""
        from zae_limiter.exceptions import ValidationError

        with patch("zae_limiter.repository.Repository") as mock_repo_class:
            mock_repo_class.side_effect = ValidationError(
                field="name", value="invalid", reason="Invalid name format"
            )

            result = runner.invoke(cli, ["resource", "get-defaults", "gpt-4"])

            assert result.exit_code == 1
            assert "Invalid name format" in result.output

    def test_resource_delete_repo_init_validation_error(self, runner: CliRunner) -> None:
        """Test resource delete-defaults handles ValidationError during Repository init."""
        from zae_limiter.exceptions import ValidationError

        with patch("zae_limiter.repository.Repository") as mock_repo_class:
            mock_repo_class.side_effect = ValidationError(
                field="name", value="invalid", reason="Invalid name format"
            )

            result = runner.invoke(cli, ["resource", "delete-defaults", "gpt-4", "--yes"])

            assert result.exit_code == 1
            assert "Invalid name format" in result.output

    def test_resource_list_repo_init_validation_error(self, runner: CliRunner) -> None:
        """Test resource list handles ValidationError during Repository init."""
        from zae_limiter.exceptions import ValidationError

        with patch("zae_limiter.repository.Repository") as mock_repo_class:
            mock_repo_class.side_effect = ValidationError(
                field="name", value="invalid", reason="Invalid name format"
            )

            result = runner.invoke(cli, ["resource", "list"])

            assert result.exit_code == 1
            assert "Invalid name format" in result.output


class TestSystemCommandsEdgeCases:
    """Test edge cases for system CLI commands."""

    @patch("zae_limiter.repository.Repository")
    def test_system_set_defaults_with_custom_name(
        self, mock_repo_class: Mock, runner: CliRunner
    ) -> None:
        """Test system set-defaults with custom --name option."""
        mock_repo = Mock()
        mock_repo.set_system_defaults = AsyncMock(return_value=None)
        mock_repo.close = AsyncMock(return_value=None)
        mock_repo_class.return_value = mock_repo

        result = runner.invoke(
            cli,
            ["system", "set-defaults", "-n", "my-limiter", "-l", "tpm:10000"],
        )

        assert result.exit_code == 0
        mock_repo_class.assert_called_once()
        call_args = mock_repo_class.call_args
        assert call_args[0][0] == "my-limiter"

    def test_system_set_defaults_repo_init_validation_error(self, runner: CliRunner) -> None:
        """Test system set-defaults handles ValidationError during Repository init."""
        from zae_limiter.exceptions import ValidationError

        with patch("zae_limiter.repository.Repository") as mock_repo_class:
            mock_repo_class.side_effect = ValidationError(
                field="name", value="invalid", reason="Invalid name format"
            )

            result = runner.invoke(cli, ["system", "set-defaults", "-l", "tpm:10000"])

            assert result.exit_code == 1
            assert "Invalid name format" in result.output

    def test_system_get_defaults_repo_init_validation_error(self, runner: CliRunner) -> None:
        """Test system get-defaults handles ValidationError during Repository init."""
        from zae_limiter.exceptions import ValidationError

        with patch("zae_limiter.repository.Repository") as mock_repo_class:
            mock_repo_class.side_effect = ValidationError(
                field="name", value="invalid", reason="Invalid name format"
            )

            result = runner.invoke(cli, ["system", "get-defaults"])

            assert result.exit_code == 1
            assert "Invalid name format" in result.output

    def test_system_delete_defaults_repo_init_validation_error(self, runner: CliRunner) -> None:
        """Test system delete-defaults handles ValidationError during Repository init."""
        from zae_limiter.exceptions import ValidationError

        with patch("zae_limiter.repository.Repository") as mock_repo_class:
            mock_repo_class.side_effect = ValidationError(
                field="name", value="invalid", reason="Invalid name format"
            )

            result = runner.invoke(cli, ["system", "delete-defaults", "--yes"])

            assert result.exit_code == 1
            assert "Invalid name format" in result.output

    @patch("zae_limiter.repository.Repository")
    def test_system_get_defaults_validation_error(
        self, mock_repo_class: Mock, runner: CliRunner
    ) -> None:
        """Test system get-defaults handles ValidationError from repository."""
        from zae_limiter.exceptions import ValidationError

        mock_repo = Mock()
        mock_repo.get_system_defaults = AsyncMock(
            side_effect=ValidationError(field="limits", value="invalid", reason="Invalid limits")
        )
        mock_repo.close = AsyncMock(return_value=None)
        mock_repo_class.return_value = mock_repo

        result = runner.invoke(cli, ["system", "get-defaults"])

        assert result.exit_code == 1
        assert "Invalid limits" in result.output

    @patch("zae_limiter.repository.Repository")
    def test_system_delete_defaults_validation_error(
        self, mock_repo_class: Mock, runner: CliRunner
    ) -> None:
        """Test system delete-defaults handles ValidationError from repository."""
        from zae_limiter.exceptions import ValidationError

        mock_repo = Mock()
        mock_repo.delete_system_defaults = AsyncMock(
            side_effect=ValidationError(field="limits", value="invalid", reason="Invalid limits")
        )
        mock_repo.close = AsyncMock(return_value=None)
        mock_repo_class.return_value = mock_repo

        result = runner.invoke(cli, ["system", "delete-defaults", "--yes"])

        assert result.exit_code == 1
        assert "Invalid limits" in result.output

    @patch("zae_limiter.repository.Repository")
    def test_resource_get_validation_error(self, mock_repo_class: Mock, runner: CliRunner) -> None:
        """Test resource get-defaults handles ValidationError from repository."""
        from zae_limiter.exceptions import ValidationError

        mock_repo = Mock()
        mock_repo.get_resource_defaults = AsyncMock(
            side_effect=ValidationError(
                field="resource", value="invalid", reason="Invalid resource name"
            )
        )
        mock_repo.close = AsyncMock(return_value=None)
        mock_repo_class.return_value = mock_repo

        result = runner.invoke(cli, ["resource", "get-defaults", "invalid!resource"])

        assert result.exit_code == 1
        assert "Invalid resource name" in result.output

    @patch("zae_limiter.repository.Repository")
    def test_resource_delete_validation_error(
        self, mock_repo_class: Mock, runner: CliRunner
    ) -> None:
        """Test resource delete-defaults handles ValidationError from repository."""
        from zae_limiter.exceptions import ValidationError

        mock_repo = Mock()
        mock_repo.delete_resource_defaults = AsyncMock(
            side_effect=ValidationError(
                field="resource", value="invalid", reason="Invalid resource name"
            )
        )
        mock_repo.close = AsyncMock(return_value=None)
        mock_repo_class.return_value = mock_repo

        result = runner.invoke(cli, ["resource", "delete-defaults", "gpt-4", "--yes"])

        assert result.exit_code == 1
        assert "Invalid resource name" in result.output


class TestEntityCommands:
    """Test entity CLI commands."""

    def test_entity_help(self, runner: CliRunner) -> None:
        """Test entity command group help."""
        result = runner.invoke(cli, ["entity", "--help"])
        assert result.exit_code == 0
        assert "Entity-level limit configuration" in result.output

    def test_entity_set_limits_help(self, runner: CliRunner) -> None:
        """Test entity set-limits command help."""
        result = runner.invoke(cli, ["entity", "set-limits", "--help"])
        assert result.exit_code == 0
        assert "Set limits for a specific entity and resource" in result.output
        assert "--limit" in result.output
        assert "--resource" in result.output
        assert "ENTITY_ID" in result.output

    def test_entity_get_limits_help(self, runner: CliRunner) -> None:
        """Test entity get-limits command help."""
        result = runner.invoke(cli, ["entity", "get-limits", "--help"])
        assert result.exit_code == 0
        assert "Get limits for a specific entity and resource" in result.output
        assert "--resource" in result.output
        assert "ENTITY_ID" in result.output

    def test_entity_delete_limits_help(self, runner: CliRunner) -> None:
        """Test entity delete-limits command help."""
        result = runner.invoke(cli, ["entity", "delete-limits", "--help"])
        assert result.exit_code == 0
        assert "Delete limits for a specific entity and resource" in result.output
        assert "--yes" in result.output
        assert "--resource" in result.output

    def test_entity_set_limits_requires_entity_id(self, runner: CliRunner) -> None:
        """Test entity set-limits requires ENTITY_ID argument."""
        result = runner.invoke(cli, ["entity", "set-limits", "-r", "gpt-4", "-l", "tpm:10000"])
        assert result.exit_code != 0
        assert "ENTITY_ID" in result.output

    @patch("zae_limiter.repository.Repository")
    def test_entity_set_limits_defaults_resource_to_default(
        self, mock_repo_class: Mock, runner: CliRunner
    ) -> None:
        """Test entity set-limits defaults --resource to _default_ (ADR-118)."""
        mock_repo = Mock()
        mock_repo.set_limits = AsyncMock(return_value=None)
        mock_repo.close = AsyncMock(return_value=None)
        mock_repo_class.return_value = mock_repo

        result = runner.invoke(cli, ["entity", "set-limits", "user-123", "-l", "tpm:10000"])
        assert result.exit_code == 0
        assert "_default_" in result.output
        # Verify set_limits called with resource="_default_"
        mock_repo.set_limits.assert_called_once()
        call_args = mock_repo.set_limits.call_args
        assert call_args.kwargs.get("resource") == "_default_"

    def test_entity_set_limits_requires_limit(self, runner: CliRunner) -> None:
        """Test entity set-limits requires at least one --limit."""
        result = runner.invoke(cli, ["entity", "set-limits", "user-123", "-r", "gpt-4"])
        assert result.exit_code != 0
        assert "limit" in result.output.lower()

    @patch("zae_limiter.repository.Repository")
    def test_entity_set_limits_success(self, mock_repo_class: Mock, runner: CliRunner) -> None:
        """Test entity set-limits with valid limits."""
        mock_repo = Mock()
        mock_repo.set_limits = AsyncMock(return_value=None)
        mock_repo.close = AsyncMock(return_value=None)
        mock_repo_class.return_value = mock_repo

        result = runner.invoke(
            cli,
            [
                "entity",
                "set-limits",
                "user-123",
                "-r",
                "gpt-4",
                "-l",
                "tpm:10000",
                "-l",
                "rpm:500:1000",
            ],
        )

        assert result.exit_code == 0
        assert "Set 2 limit(s) for entity 'user-123'" in result.output
        assert "gpt-4" in result.output
        assert "tpm: 10,000/min (burst: 10,000)" in result.output
        assert "rpm: 500/min (burst: 1,000)" in result.output

        # Verify repository was called correctly
        mock_repo.set_limits.assert_called_once()
        call_args = mock_repo.set_limits.call_args
        assert call_args[0][0] == "user-123"
        limits = call_args[0][1]
        assert len(limits) == 2
        assert limits[0].name == "tpm"
        assert limits[0].capacity == 10000
        assert limits[1].name == "rpm"
        assert limits[1].capacity == 500
        assert limits[1].burst == 1000
        assert call_args[1]["resource"] == "gpt-4"

    def test_entity_set_limits_invalid_limit_format(self, runner: CliRunner) -> None:
        """Test entity set-limits with invalid limit format."""
        result = runner.invoke(
            cli, ["entity", "set-limits", "user-123", "-r", "gpt-4", "-l", "invalid"]
        )

        assert result.exit_code == 1
        assert "Invalid limit format" in result.output

    def test_entity_set_limits_invalid_limit_values(self, runner: CliRunner) -> None:
        """Test entity set-limits with non-numeric limit values."""
        result = runner.invoke(
            cli, ["entity", "set-limits", "user-123", "-r", "gpt-4", "-l", "tpm:abc"]
        )

        assert result.exit_code == 1
        assert "Invalid limit values" in result.output

    @patch("zae_limiter.repository.Repository")
    def test_entity_set_limits_validation_error(
        self, mock_repo_class: Mock, runner: CliRunner
    ) -> None:
        """Test entity set-limits handles ValidationError."""
        from zae_limiter.exceptions import ValidationError

        mock_repo = Mock()
        mock_repo.set_limits = AsyncMock(
            side_effect=ValidationError(
                field="entity_id", value="user-123", reason="Invalid entity ID"
            )
        )
        mock_repo.close = AsyncMock(return_value=None)
        mock_repo_class.return_value = mock_repo

        result = runner.invoke(
            cli, ["entity", "set-limits", "user-123", "-r", "gpt-4", "-l", "tpm:10000"]
        )

        assert result.exit_code == 1
        assert "Invalid entity ID" in result.output

    @patch("zae_limiter.repository.Repository")
    def test_entity_set_limits_handles_exception(
        self, mock_repo_class: Mock, runner: CliRunner
    ) -> None:
        """Test entity set-limits handles unexpected exceptions."""
        mock_repo = Mock()
        mock_repo.set_limits = AsyncMock(side_effect=Exception("DynamoDB error"))
        mock_repo.close = AsyncMock(return_value=None)
        mock_repo_class.return_value = mock_repo

        result = runner.invoke(
            cli, ["entity", "set-limits", "user-123", "-r", "gpt-4", "-l", "tpm:10000"]
        )

        assert result.exit_code == 1
        assert "Failed to set entity limits" in result.output

    @patch("zae_limiter.repository.Repository")
    def test_entity_get_limits_with_limits(self, mock_repo_class: Mock, runner: CliRunner) -> None:
        """Test entity get-limits displays limits."""
        from zae_limiter.models import Limit

        mock_limits = [
            Limit(
                name="tpm",
                capacity=10000,
                burst=10000,
                refill_amount=10000,
                refill_period_seconds=60,
            ),
            Limit(
                name="rpm",
                capacity=500,
                burst=1000,
                refill_amount=500,
                refill_period_seconds=60,
            ),
        ]

        mock_repo = Mock()
        mock_repo.get_limits = AsyncMock(return_value=mock_limits)
        mock_repo.close = AsyncMock(return_value=None)
        mock_repo_class.return_value = mock_repo

        result = runner.invoke(cli, ["entity", "get-limits", "user-123", "-r", "gpt-4"])

        assert result.exit_code == 0
        assert "Limits for entity 'user-123'" in result.output
        assert "gpt-4" in result.output
        assert "tpm: 10,000/min (burst: 10,000)" in result.output
        assert "rpm: 500/min (burst: 1,000)" in result.output

    @patch("zae_limiter.repository.Repository")
    def test_entity_get_limits_no_limits(self, mock_repo_class: Mock, runner: CliRunner) -> None:
        """Test entity get-limits when no limits configured."""
        mock_repo = Mock()
        mock_repo.get_limits = AsyncMock(return_value=[])
        mock_repo.close = AsyncMock(return_value=None)
        mock_repo_class.return_value = mock_repo

        result = runner.invoke(cli, ["entity", "get-limits", "user-123", "-r", "gpt-4"])

        assert result.exit_code == 0
        assert "No limits configured for entity 'user-123'" in result.output
        assert "gpt-4" in result.output

    @patch("zae_limiter.repository.Repository")
    def test_entity_get_limits_validation_error(
        self, mock_repo_class: Mock, runner: CliRunner
    ) -> None:
        """Test entity get-limits handles ValidationError."""
        from zae_limiter.exceptions import ValidationError

        mock_repo = Mock()
        mock_repo.get_limits = AsyncMock(
            side_effect=ValidationError(
                field="entity_id", value="user-123", reason="Invalid entity ID"
            )
        )
        mock_repo.close = AsyncMock(return_value=None)
        mock_repo_class.return_value = mock_repo

        result = runner.invoke(cli, ["entity", "get-limits", "user-123", "-r", "gpt-4"])

        assert result.exit_code == 1
        assert "Invalid entity ID" in result.output

    @patch("zae_limiter.repository.Repository")
    def test_entity_get_limits_handles_exception(
        self, mock_repo_class: Mock, runner: CliRunner
    ) -> None:
        """Test entity get-limits handles unexpected exceptions."""
        mock_repo = Mock()
        mock_repo.get_limits = AsyncMock(side_effect=Exception("Connection failed"))
        mock_repo.close = AsyncMock(return_value=None)
        mock_repo_class.return_value = mock_repo

        result = runner.invoke(cli, ["entity", "get-limits", "user-123", "-r", "gpt-4"])

        assert result.exit_code == 1
        assert "Failed to get entity limits" in result.output

    @patch("zae_limiter.repository.Repository")
    def test_entity_delete_limits_with_confirmation(
        self, mock_repo_class: Mock, runner: CliRunner
    ) -> None:
        """Test entity delete-limits with user confirmation."""
        mock_repo = Mock()
        mock_repo.delete_limits = AsyncMock(return_value=None)
        mock_repo.close = AsyncMock(return_value=None)
        mock_repo_class.return_value = mock_repo

        result = runner.invoke(
            cli, ["entity", "delete-limits", "user-123", "-r", "gpt-4"], input="y\n"
        )

        assert result.exit_code == 0
        assert "Deleted limits for entity 'user-123'" in result.output
        assert "gpt-4" in result.output

    @patch("zae_limiter.repository.Repository")
    def test_entity_delete_limits_cancelled(self, mock_repo_class: Mock, runner: CliRunner) -> None:
        """Test entity delete-limits cancelled by user."""
        result = runner.invoke(
            cli, ["entity", "delete-limits", "user-123", "-r", "gpt-4"], input="n\n"
        )

        assert result.exit_code == 0
        assert "Cancelled" in result.output

    @patch("zae_limiter.repository.Repository")
    def test_entity_delete_limits_with_yes_flag(
        self, mock_repo_class: Mock, runner: CliRunner
    ) -> None:
        """Test entity delete-limits with --yes flag skips confirmation."""
        mock_repo = Mock()
        mock_repo.delete_limits = AsyncMock(return_value=None)
        mock_repo.close = AsyncMock(return_value=None)
        mock_repo_class.return_value = mock_repo

        result = runner.invoke(cli, ["entity", "delete-limits", "user-123", "-r", "gpt-4", "--yes"])

        assert result.exit_code == 0
        assert "Deleted limits for entity 'user-123'" in result.output
        assert "gpt-4" in result.output

    @patch("zae_limiter.repository.Repository")
    def test_entity_delete_limits_validation_error(
        self, mock_repo_class: Mock, runner: CliRunner
    ) -> None:
        """Test entity delete-limits handles ValidationError."""
        from zae_limiter.exceptions import ValidationError

        mock_repo = Mock()
        mock_repo.delete_limits = AsyncMock(
            side_effect=ValidationError(
                field="entity_id", value="user-123", reason="Invalid entity ID"
            )
        )
        mock_repo.close = AsyncMock(return_value=None)
        mock_repo_class.return_value = mock_repo

        result = runner.invoke(cli, ["entity", "delete-limits", "user-123", "-r", "gpt-4", "--yes"])

        assert result.exit_code == 1
        assert "Invalid entity ID" in result.output

    @patch("zae_limiter.repository.Repository")
    def test_entity_delete_limits_handles_exception(
        self, mock_repo_class: Mock, runner: CliRunner
    ) -> None:
        """Test entity delete-limits handles unexpected exceptions."""
        mock_repo = Mock()
        mock_repo.delete_limits = AsyncMock(side_effect=Exception("DynamoDB error"))
        mock_repo.close = AsyncMock(return_value=None)
        mock_repo_class.return_value = mock_repo

        result = runner.invoke(cli, ["entity", "delete-limits", "user-123", "-r", "gpt-4", "--yes"])

        assert result.exit_code == 1
        assert "Failed to delete entity limits" in result.output


class TestEntityCommandsEdgeCases:
    """Test edge cases for entity CLI commands."""

    @patch("zae_limiter.repository.Repository")
    def test_entity_set_limits_with_custom_name(
        self, mock_repo_class: Mock, runner: CliRunner
    ) -> None:
        """Test entity set-limits with custom stack name."""
        mock_repo = Mock()
        mock_repo.set_limits = AsyncMock(return_value=None)
        mock_repo.close = AsyncMock(return_value=None)
        mock_repo_class.return_value = mock_repo

        result = runner.invoke(
            cli,
            [
                "entity",
                "set-limits",
                "user-123",
                "-r",
                "gpt-4",
                "-l",
                "tpm:10000",
                "--name",
                "custom-stack",
            ],
        )

        assert result.exit_code == 0
        # Verify Repository was called with custom name
        mock_repo_class.assert_called_once_with("custom-stack", None, None)

    @patch("zae_limiter.repository.Repository")
    def test_entity_set_limits_with_region(self, mock_repo_class: Mock, runner: CliRunner) -> None:
        """Test entity set-limits with region."""
        mock_repo = Mock()
        mock_repo.set_limits = AsyncMock(return_value=None)
        mock_repo.close = AsyncMock(return_value=None)
        mock_repo_class.return_value = mock_repo

        result = runner.invoke(
            cli,
            [
                "entity",
                "set-limits",
                "user-123",
                "-r",
                "gpt-4",
                "-l",
                "tpm:10000",
                "--region",
                "us-west-2",
            ],
        )

        assert result.exit_code == 0
        # Verify Repository was called with region
        mock_repo_class.assert_called_once_with("limiter", "us-west-2", None)

    @patch("zae_limiter.repository.Repository")
    def test_entity_set_limits_with_endpoint_url(
        self, mock_repo_class: Mock, runner: CliRunner
    ) -> None:
        """Test entity set-limits with endpoint URL."""
        mock_repo = Mock()
        mock_repo.set_limits = AsyncMock(return_value=None)
        mock_repo.close = AsyncMock(return_value=None)
        mock_repo_class.return_value = mock_repo

        result = runner.invoke(
            cli,
            [
                "entity",
                "set-limits",
                "user-123",
                "-r",
                "gpt-4",
                "-l",
                "tpm:10000",
                "--endpoint-url",
                "http://localhost:4566",
            ],
        )

        assert result.exit_code == 0
        # Verify Repository was called with endpoint_url
        mock_repo_class.assert_called_once_with("limiter", None, "http://localhost:4566")

    def test_entity_set_limits_repo_init_validation_error(self, runner: CliRunner) -> None:
        """Test entity set-limits handles ValidationError during Repository init."""
        with patch("zae_limiter.repository.Repository") as mock_repo_class:
            from zae_limiter.exceptions import ValidationError

            mock_repo_class.side_effect = ValidationError(
                field="name", value="invalid_name", reason="Invalid name format"
            )

            result = runner.invoke(
                cli, ["entity", "set-limits", "user-123", "-r", "gpt-4", "-l", "tpm:10000"]
            )

            assert result.exit_code == 1
            assert "Invalid name format" in result.output

    def test_entity_get_limits_repo_init_validation_error(self, runner: CliRunner) -> None:
        """Test entity get-limits handles ValidationError during Repository init."""
        with patch("zae_limiter.repository.Repository") as mock_repo_class:
            from zae_limiter.exceptions import ValidationError

            mock_repo_class.side_effect = ValidationError(
                field="name", value="invalid_name", reason="Invalid name format"
            )

            result = runner.invoke(cli, ["entity", "get-limits", "user-123", "-r", "gpt-4"])

            assert result.exit_code == 1
            assert "Invalid name format" in result.output

    def test_entity_delete_limits_repo_init_validation_error(self, runner: CliRunner) -> None:
        """Test entity delete-limits handles ValidationError during Repository init."""
        with patch("zae_limiter.repository.Repository") as mock_repo_class:
            from zae_limiter.exceptions import ValidationError

            mock_repo_class.side_effect = ValidationError(
                field="name", value="invalid_name", reason="Invalid name format"
            )

            result = runner.invoke(
                cli, ["entity", "delete-limits", "user-123", "-r", "gpt-4", "--yes"]
            )

            assert result.exit_code == 1
            assert "Invalid name format" in result.output

    # ---- entity create tests ----

    def test_entity_create_help(self, runner: CliRunner) -> None:
        """Test entity create command help."""
        result = runner.invoke(cli, ["entity", "create", "--help"])
        assert result.exit_code == 0
        assert "Create a new entity" in result.output
        assert "--cascade" in result.output
        assert "--parent" in result.output

    @patch("zae_limiter.repository.Repository")
    def test_entity_create_success(self, mock_repo_class: Mock, runner: CliRunner) -> None:
        """Test entity create with minimal args."""
        from zae_limiter.models import Entity

        mock_repo = Mock()
        mock_repo.create_entity = AsyncMock(
            return_value=Entity(id="user-123", name="user-123", cascade=False)
        )
        mock_repo.close = AsyncMock()
        mock_repo_class.return_value = mock_repo

        result = runner.invoke(cli, ["entity", "create", "user-123"])

        assert result.exit_code == 0
        assert "Created entity 'user-123'" in result.output
        assert "Cascade: False" in result.output
        mock_repo.create_entity.assert_called_once_with(
            entity_id="user-123", name=None, parent_id=None, cascade=False
        )

    @patch("zae_limiter.repository.Repository")
    def test_entity_create_with_cascade(self, mock_repo_class: Mock, runner: CliRunner) -> None:
        """Test entity create with --cascade flag."""
        from zae_limiter.models import Entity

        mock_repo = Mock()
        mock_repo.create_entity = AsyncMock(
            return_value=Entity(id="key-1", name="key-1", parent_id="proj-1", cascade=True)
        )
        mock_repo.close = AsyncMock()
        mock_repo_class.return_value = mock_repo

        result = runner.invoke(
            cli, ["entity", "create", "key-1", "--parent", "proj-1", "--cascade"]
        )

        assert result.exit_code == 0
        assert "Created entity 'key-1'" in result.output
        assert "Parent:  proj-1" in result.output
        assert "Cascade: True" in result.output
        mock_repo.create_entity.assert_called_once_with(
            entity_id="key-1", name=None, parent_id="proj-1", cascade=True
        )

    @patch("zae_limiter.repository.Repository")
    def test_entity_create_with_display_name(
        self, mock_repo_class: Mock, runner: CliRunner
    ) -> None:
        """Test entity create with --display-name."""
        from zae_limiter.models import Entity

        mock_repo = Mock()
        mock_repo.create_entity = AsyncMock(
            return_value=Entity(id="proj-1", name="ACME Project", cascade=False)
        )
        mock_repo.close = AsyncMock()
        mock_repo_class.return_value = mock_repo

        result = runner.invoke(
            cli, ["entity", "create", "proj-1", "--display-name", "ACME Project"]
        )

        assert result.exit_code == 0
        assert "Created entity 'proj-1'" in result.output
        assert "Name:    ACME Project" in result.output

    @patch("zae_limiter.repository.Repository")
    def test_entity_create_handles_exception(
        self, mock_repo_class: Mock, runner: CliRunner
    ) -> None:
        """Test entity create handles unexpected exceptions."""
        mock_repo = Mock()
        mock_repo.create_entity = AsyncMock(side_effect=Exception("DynamoDB error"))
        mock_repo.close = AsyncMock()
        mock_repo_class.return_value = mock_repo

        result = runner.invoke(cli, ["entity", "create", "user-123"])

        assert result.exit_code == 1
        assert "DynamoDB error" in result.output

    def test_entity_create_repo_init_validation_error(self, runner: CliRunner) -> None:
        """Test entity create handles ValidationError during Repository init."""
        with patch("zae_limiter.repository.Repository") as mock_repo_class:
            from zae_limiter.exceptions import ValidationError

            mock_repo_class.side_effect = ValidationError(
                field="name", value="invalid", reason="Invalid name format"
            )

            result = runner.invoke(cli, ["entity", "create", "user-123"])

            assert result.exit_code == 1
            assert "Invalid name format" in result.output

    # ---- entity show tests ----

    def test_entity_show_help(self, runner: CliRunner) -> None:
        """Test entity show command help."""
        result = runner.invoke(cli, ["entity", "show", "--help"])
        assert result.exit_code == 0
        assert "Show details for an entity" in result.output

    @patch("zae_limiter.repository.Repository")
    def test_entity_show_success(self, mock_repo_class: Mock, runner: CliRunner) -> None:
        """Test entity show displays entity details."""
        from zae_limiter.models import Entity

        mock_repo = Mock()
        mock_repo.get_entity = AsyncMock(
            return_value=Entity(
                id="key-1",
                name="API Key 1",
                parent_id="proj-1",
                cascade=True,
                created_at="2026-01-25T00:00:00Z",
                metadata={"tier": "premium"},
            )
        )
        mock_repo.close = AsyncMock()
        mock_repo_class.return_value = mock_repo

        result = runner.invoke(cli, ["entity", "show", "key-1"])

        assert result.exit_code == 0
        assert "Entity: key-1" in result.output
        assert "Name:       API Key 1" in result.output
        assert "Parent:     proj-1" in result.output
        assert "Cascade:    True" in result.output
        assert "Created:    2026-01-25T00:00:00Z" in result.output

    @patch("zae_limiter.repository.Repository")
    def test_entity_show_not_found(self, mock_repo_class: Mock, runner: CliRunner) -> None:
        """Test entity show when entity doesn't exist."""
        mock_repo = Mock()
        mock_repo.get_entity = AsyncMock(return_value=None)
        mock_repo.close = AsyncMock()
        mock_repo_class.return_value = mock_repo

        result = runner.invoke(cli, ["entity", "show", "missing-entity"])

        assert result.exit_code == 1
        assert "Entity 'missing-entity' not found" in result.output

    @patch("zae_limiter.repository.Repository")
    def test_entity_show_handles_exception(self, mock_repo_class: Mock, runner: CliRunner) -> None:
        """Test entity show handles unexpected exceptions."""
        mock_repo = Mock()
        mock_repo.get_entity = AsyncMock(side_effect=Exception("Connection failed"))
        mock_repo.close = AsyncMock()
        mock_repo_class.return_value = mock_repo

        result = runner.invoke(cli, ["entity", "show", "user-123"])

        assert result.exit_code == 1
        assert "Connection failed" in result.output

    def test_entity_show_repo_init_validation_error(self, runner: CliRunner) -> None:
        """Test entity show handles ValidationError during Repository init."""
        with patch("zae_limiter.repository.Repository") as mock_repo_class:
            from zae_limiter.exceptions import ValidationError

            mock_repo_class.side_effect = ValidationError(
                field="name", value="invalid", reason="Invalid name format"
            )

            result = runner.invoke(cli, ["entity", "show", "user-123"])

            assert result.exit_code == 1
            assert "Invalid name format" in result.output


class TestEntityListCommand:
    """Test entity list command (GSI3 sparse index queries)."""

    def test_entity_list_help(self, runner: CliRunner) -> None:
        """Test entity list command help."""
        result = runner.invoke(cli, ["entity", "list", "--help"])
        assert result.exit_code == 0
        assert "--with-custom-limits" in result.output
        assert "List entities with custom limit configurations" in result.output

    @patch("zae_limiter.repository.Repository")
    def test_entity_list_with_custom_limits(self, mock_repo_class: Mock, runner: CliRunner) -> None:
        """Test entity list --with-custom-limits returns entities."""
        mock_repo = Mock()
        mock_repo.list_entities_with_custom_limits = AsyncMock(
            return_value=(["entity-1", "entity-2"], None)
        )
        mock_repo.close = AsyncMock()
        mock_repo_class.return_value = mock_repo

        result = runner.invoke(cli, ["entity", "list", "--with-custom-limits", "gpt-4"])

        assert result.exit_code == 0
        assert "entity-1" in result.output
        assert "entity-2" in result.output
        mock_repo.list_entities_with_custom_limits.assert_called()

    @patch("zae_limiter.repository.Repository")
    def test_entity_list_with_limit_option(self, mock_repo_class: Mock, runner: CliRunner) -> None:
        """Test entity list --with-custom-limits with --limit option."""
        mock_repo = Mock()
        mock_repo.list_entities_with_custom_limits = AsyncMock(return_value=(["entity-1"], None))
        mock_repo.close = AsyncMock()
        mock_repo_class.return_value = mock_repo

        result = runner.invoke(
            cli, ["entity", "list", "--with-custom-limits", "gpt-4", "--limit", "1"]
        )

        assert result.exit_code == 0
        assert "entity-1" in result.output
        # Verify limit parameter was passed
        call_args = mock_repo.list_entities_with_custom_limits.call_args
        assert call_args[1].get("limit") == 1 or call_args[0][1] == 1

    @patch("zae_limiter.repository.Repository")
    def test_entity_list_no_results(self, mock_repo_class: Mock, runner: CliRunner) -> None:
        """Test entity list --with-custom-limits with no results."""
        mock_repo = Mock()
        mock_repo.list_entities_with_custom_limits = AsyncMock(return_value=([], None))
        mock_repo.close = AsyncMock()
        mock_repo_class.return_value = mock_repo

        result = runner.invoke(cli, ["entity", "list", "--with-custom-limits", "gpt-4"])

        assert result.exit_code == 0
        # Output should be empty or minimal

    @patch("zae_limiter.repository.Repository")
    def test_entity_list_handles_exception(self, mock_repo_class: Mock, runner: CliRunner) -> None:
        """Test entity list handles exceptions gracefully."""
        mock_repo = Mock()
        mock_repo.list_entities_with_custom_limits = AsyncMock(
            side_effect=Exception("Connection failed")
        )
        mock_repo.close = AsyncMock()
        mock_repo_class.return_value = mock_repo

        result = runner.invoke(cli, ["entity", "list", "--with-custom-limits", "gpt-4"])

        assert result.exit_code == 1
        assert "Connection failed" in result.output

    def test_entity_list_requires_with_custom_limits(self, runner: CliRunner) -> None:
        """Test entity list requires --with-custom-limits option."""
        result = runner.invoke(cli, ["entity", "list"])
        assert result.exit_code != 0
        assert "with-custom-limits" in result.output.lower()

    def test_entity_list_repo_init_validation_error(self, runner: CliRunner) -> None:
        """Test entity list handles ValidationError during Repository init."""
        with patch("zae_limiter.repository.Repository") as mock_repo_class:
            from zae_limiter.exceptions import ValidationError

            mock_repo_class.side_effect = ValidationError(
                field="name", value="invalid", reason="Invalid name format"
            )

            result = runner.invoke(cli, ["entity", "list", "--with-custom-limits", "gpt-4"])

            assert result.exit_code == 1
            assert "Invalid name format" in result.output

    @patch("zae_limiter.repository.Repository")
    def test_entity_list_query_validation_error(
        self, mock_repo_class: Mock, runner: CliRunner
    ) -> None:
        """Test entity list handles ValidationError during query."""
        from zae_limiter.exceptions import ValidationError

        mock_repo = Mock()
        mock_repo.list_entities_with_custom_limits = AsyncMock(
            side_effect=ValidationError(
                field="resource", value="bad#name", reason="Invalid resource name"
            )
        )
        mock_repo.close = AsyncMock()
        mock_repo_class.return_value = mock_repo

        result = runner.invoke(cli, ["entity", "list", "--with-custom-limits", "bad#name"])

        assert result.exit_code == 1
        assert "Invalid resource name" in result.output


class TestEntityListResourcesCommand:
    """Test entity list-resources command."""

    def test_entity_list_resources_help(self, runner: CliRunner) -> None:
        """Test entity list-resources command help."""
        result = runner.invoke(cli, ["entity", "list-resources", "--help"])
        assert result.exit_code == 0
        assert "List resources with entity-level custom limit configurations" in result.output

    @patch("zae_limiter.repository.Repository")
    def test_entity_list_resources_returns_results(
        self, mock_repo_class: Mock, runner: CliRunner
    ) -> None:
        """Test entity list-resources returns resource names."""
        mock_repo = Mock()
        mock_repo.list_resources_with_entity_configs = AsyncMock(return_value=["claude-3", "gpt-4"])
        mock_repo.close = AsyncMock()
        mock_repo_class.return_value = mock_repo

        result = runner.invoke(cli, ["entity", "list-resources", "--name", "test-limiter"])

        assert result.exit_code == 0
        assert "gpt-4" in result.output
        assert "claude-3" in result.output
        mock_repo.list_resources_with_entity_configs.assert_called_once()

    @patch("zae_limiter.repository.Repository")
    def test_entity_list_resources_empty(self, mock_repo_class: Mock, runner: CliRunner) -> None:
        """Test entity list-resources with no results."""
        mock_repo = Mock()
        mock_repo.list_resources_with_entity_configs = AsyncMock(return_value=[])
        mock_repo.close = AsyncMock()
        mock_repo_class.return_value = mock_repo

        result = runner.invoke(cli, ["entity", "list-resources", "--name", "test-limiter"])

        assert result.exit_code == 0
        assert "No resources with entity-level custom limits" in result.output

    @patch("zae_limiter.repository.Repository")
    def test_entity_list_resources_handles_exception(
        self, mock_repo_class: Mock, runner: CliRunner
    ) -> None:
        """Test entity list-resources handles exceptions gracefully."""
        mock_repo = Mock()
        mock_repo.list_resources_with_entity_configs = AsyncMock(
            side_effect=Exception("Connection failed")
        )
        mock_repo.close = AsyncMock()
        mock_repo_class.return_value = mock_repo

        result = runner.invoke(cli, ["entity", "list-resources", "--name", "test-limiter"])

        assert result.exit_code == 1
        assert "Connection failed" in result.output

    def test_entity_list_resources_validation_error(self, runner: CliRunner) -> None:
        """Test entity list-resources handles ValidationError during Repository init."""
        with patch("zae_limiter.repository.Repository") as mock_repo_class:
            from zae_limiter.exceptions import ValidationError

            mock_repo_class.side_effect = ValidationError("name", "bad_name", "Invalid name format")

            result = runner.invoke(cli, ["entity", "list-resources", "--name", "bad_name"])

            assert result.exit_code == 1
            assert "Invalid name format" in result.output
