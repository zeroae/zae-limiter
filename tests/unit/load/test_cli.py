"""Tests for load test CLI commands."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from click.testing import CliRunner

from zae_limiter.load.cli import load


@pytest.fixture()
def runner():
    return CliRunner()


# ---------------------------------------------------------------------------
# Interactive selection helpers
# ---------------------------------------------------------------------------


class TestSelectName:
    """Tests for _select_name interactive selector."""

    def test_returns_selected_stack(self):
        from zae_limiter.load.cli import _select_name

        mock_discovery = MagicMock()
        mock_discovery.__aenter__ = AsyncMock(return_value=mock_discovery)
        mock_discovery.__aexit__ = AsyncMock(return_value=False)

        mock_info = MagicMock()
        mock_info.stack_name = "my-app"
        mock_info.stack_status = "CREATE_COMPLETE"
        mock_discovery.list_limiters = AsyncMock(return_value=[mock_info])

        mock_questionary = MagicMock()
        mock_questionary.select.return_value.ask.return_value = "my-app"

        with (
            patch.dict(
                "sys.modules",
                {"questionary": mock_questionary},
            ),
            patch(
                "zae_limiter.infra.discovery.InfrastructureDiscovery",
                return_value=mock_discovery,
            ),
        ):
            result = _select_name("us-east-1")
            assert result == "my-app"

    def test_exits_when_no_stacks(self):
        from zae_limiter.load.cli import _select_name

        mock_discovery = MagicMock()
        mock_discovery.__aenter__ = AsyncMock(return_value=mock_discovery)
        mock_discovery.__aexit__ = AsyncMock(return_value=False)
        mock_discovery.list_limiters = AsyncMock(return_value=[])

        with (
            patch(
                "zae_limiter.infra.discovery.InfrastructureDiscovery",
                return_value=mock_discovery,
            ),
            pytest.raises(SystemExit),
        ):
            _select_name("us-east-1")


class TestSelectVpc:
    """Tests for _select_vpc interactive selector."""

    def test_returns_selected_vpc(self):
        from zae_limiter.load.cli import _select_vpc

        mock_questionary = MagicMock()
        mock_questionary.select.return_value.ask.return_value = "vpc-123"

        with (
            patch("boto3.client") as mock_client,
            patch.dict("sys.modules", {"questionary": mock_questionary}),
        ):
            mock_ec2 = MagicMock()
            mock_client.return_value = mock_ec2
            mock_ec2.describe_vpcs.return_value = {
                "Vpcs": [
                    {"VpcId": "vpc-123", "Tags": [{"Key": "Name", "Value": "main"}]},
                ]
            }
            result = _select_vpc("us-east-1")
            assert result == "vpc-123"

    def test_exits_when_no_vpcs(self):
        from zae_limiter.load.cli import _select_vpc

        with patch("boto3.client") as mock_client:
            mock_ec2 = MagicMock()
            mock_client.return_value = mock_ec2
            mock_ec2.describe_vpcs.return_value = {"Vpcs": []}
            with pytest.raises(SystemExit):
                _select_vpc("us-east-1")

    def test_vpc_without_name_tag(self):
        from zae_limiter.load.cli import _select_vpc

        mock_questionary = MagicMock()
        mock_questionary.select.return_value.ask.return_value = "vpc-456"

        with (
            patch("boto3.client") as mock_client,
            patch.dict("sys.modules", {"questionary": mock_questionary}),
        ):
            mock_ec2 = MagicMock()
            mock_client.return_value = mock_ec2
            mock_ec2.describe_vpcs.return_value = {"Vpcs": [{"VpcId": "vpc-456"}]}
            result = _select_vpc("us-east-1")
            assert result == "vpc-456"


class TestSelectSubnets:
    """Tests for _select_subnets interactive selector."""

    def test_returns_comma_separated_subnets(self):
        from zae_limiter.load.cli import _select_subnets

        mock_questionary = MagicMock()
        mock_questionary.checkbox.return_value.ask.return_value = [
            "subnet-a",
            "subnet-b",
        ]

        with (
            patch("boto3.client") as mock_client,
            patch.dict("sys.modules", {"questionary": mock_questionary}),
        ):
            mock_ec2 = MagicMock()
            mock_client.return_value = mock_ec2
            mock_ec2.describe_subnets.return_value = {
                "Subnets": [
                    {"SubnetId": "subnet-a", "AvailabilityZone": "us-east-1a"},
                    {"SubnetId": "subnet-b", "AvailabilityZone": "us-east-1b"},
                ]
            }
            result = _select_subnets("us-east-1", "vpc-123")
            assert result == "subnet-a,subnet-b"

    def test_exits_when_no_subnets(self):
        from zae_limiter.load.cli import _select_subnets

        with patch("boto3.client") as mock_client:
            mock_ec2 = MagicMock()
            mock_client.return_value = mock_ec2
            mock_ec2.describe_subnets.return_value = {"Subnets": []}
            with pytest.raises(SystemExit):
                _select_subnets("us-east-1", "vpc-123")

    def test_exits_when_fewer_than_two_selected(self):
        from zae_limiter.load.cli import _select_subnets

        mock_questionary = MagicMock()
        mock_questionary.checkbox.return_value.ask.return_value = ["subnet-a"]

        with (
            patch("boto3.client") as mock_client,
            patch.dict("sys.modules", {"questionary": mock_questionary}),
        ):
            mock_ec2 = MagicMock()
            mock_client.return_value = mock_ec2
            mock_ec2.describe_subnets.return_value = {
                "Subnets": [
                    {"SubnetId": "subnet-a", "AvailabilityZone": "us-east-1a"},
                    {"SubnetId": "subnet-b", "AvailabilityZone": "us-east-1b"},
                ]
            }
            with pytest.raises(SystemExit):
                _select_subnets("us-east-1", "vpc-123")


# ---------------------------------------------------------------------------
# CLI commands
# ---------------------------------------------------------------------------


class TestDeployCommand:
    """Tests for the deploy command."""

    def _deploy_base_mocks(self):
        """Helper returning common deploy mocks and a client_factory."""
        mock_cfn = MagicMock()
        mock_lambda_client = MagicMock()

        def client_factory(service, **kwargs):
            if service == "cloudformation":
                return mock_cfn
            elif service == "lambda":
                return mock_lambda_client
            return MagicMock()

        mock_cfn.exceptions.ClientError = Exception
        mock_cfn.exceptions.AlreadyExistsException = type(
            "AlreadyExistsException", (Exception,), {}
        )
        mock_cfn.describe_stacks.return_value = {
            "Stacks": [
                {
                    "Outputs": [
                        {
                            "OutputKey": "AppPolicyArn",
                            "OutputValue": "arn:aws:iam::123:policy/app",
                        },
                        {
                            "OutputKey": "AdminPolicyArn",
                            "OutputValue": "arn:aws:iam::123:policy/admin",
                        },
                    ]
                }
            ]
        }
        return mock_cfn, mock_lambda_client, client_factory

    def test_validates_target_stack(self, runner):
        """Deploy validates that target stack exists."""
        with patch("boto3.client") as mock_client:
            mock_cfn = MagicMock()
            mock_client.return_value = mock_cfn
            mock_cfn.exceptions.ClientError = Exception
            mock_cfn.describe_stacks.side_effect = Exception("not found")

            result = runner.invoke(
                load,
                [
                    "deploy",
                    "--name",
                    "my-app",
                    "--vpc-id",
                    "vpc-123",
                    "--subnet-ids",
                    "subnet-a,subnet-b",
                ],
            )
            assert result.exit_code != 0

    def test_checks_required_outputs(self, runner):
        """Deploy fails if stack missing required outputs."""
        with patch("boto3.client") as mock_client:
            mock_cfn = MagicMock()
            mock_client.return_value = mock_cfn
            mock_cfn.exceptions.ClientError = Exception
            mock_cfn.describe_stacks.return_value = {"Stacks": [{"Outputs": []}]}

            result = runner.invoke(
                load,
                [
                    "deploy",
                    "--name",
                    "my-app",
                    "--vpc-id",
                    "vpc-123",
                    "--subnet-ids",
                    "subnet-a,subnet-b",
                ],
            )
            assert result.exit_code != 0
            assert "missing outputs" in result.output.lower()

    def test_successful_deploy(self, runner, tmp_path):
        """Deploy creates stack and uploads Lambda code."""
        locustfile = tmp_path / "locustfile.py"
        locustfile.write_text("# locust code")

        with (
            patch("boto3.client") as mock_client,
            patch("zae_limiter.load.builder.build_and_push_locust_image") as mock_build,
            patch("zae_limiter.load.lambda_builder.build_load_lambda_package") as mock_lambda_pkg,
            patch("zae_limiter.load.builder.get_zae_limiter_source") as mock_source,
        ):
            mock_cfn = MagicMock()
            mock_lambda_client = MagicMock()

            def client_factory(service, **kwargs):
                if service == "cloudformation":
                    return mock_cfn
                elif service == "lambda":
                    return mock_lambda_client
                return MagicMock()

            mock_client.side_effect = client_factory
            mock_cfn.exceptions.ClientError = Exception
            mock_cfn.exceptions.AlreadyExistsException = type(
                "AlreadyExistsException", (Exception,), {}
            )
            mock_cfn.describe_stacks.return_value = {
                "Stacks": [
                    {
                        "Outputs": [
                            {
                                "OutputKey": "AppPolicyArn",
                                "OutputValue": "arn:aws:iam::123:policy/app",
                            },
                            {
                                "OutputKey": "AdminPolicyArn",
                                "OutputValue": "arn:aws:iam::123:policy/admin",
                            },
                        ]
                    }
                ]
            }
            mock_source.return_value = "0.8.0"
            mock_build.return_value = "123.dkr.ecr.us-east-1.amazonaws.com/test:latest"

            zip_path = tmp_path / "lambda.zip"
            zip_path.write_bytes(b"fake zip")
            mock_lambda_pkg.return_value = zip_path

            result = runner.invoke(
                load,
                [
                    "deploy",
                    "--name",
                    "my-app",
                    "--region",
                    "us-east-1",
                    "--vpc-id",
                    "vpc-123",
                    "--subnet-ids",
                    "subnet-a,subnet-b",
                    "-C",
                    str(tmp_path),
                ],
            )
            assert result.exit_code == 0, result.output
            assert "Stack ready" in result.output

    def test_interactive_fallback_for_name(self, runner, tmp_path):
        """Deploy calls _select_name when --name not provided."""
        with (
            patch("boto3.client") as mock_client,
            patch("zae_limiter.load.cli._select_name", return_value="my-app") as mock_select_name,
            patch("zae_limiter.load.builder.build_and_push_locust_image") as mock_build,
            patch("zae_limiter.load.lambda_builder.build_load_lambda_package") as mock_lambda_pkg,
            patch("zae_limiter.load.builder.get_zae_limiter_source") as mock_source,
        ):
            mock_cfn, mock_lambda_client, client_factory = self._deploy_base_mocks()
            mock_client.side_effect = client_factory

            mock_source.return_value = "0.8.0"
            mock_build.return_value = "123.dkr.ecr.us-east-1.amazonaws.com/test:latest"
            zip_path = tmp_path / "lambda.zip"
            zip_path.write_bytes(b"fake zip")
            mock_lambda_pkg.return_value = zip_path

            result = runner.invoke(
                load,
                [
                    "deploy",
                    "--vpc-id",
                    "vpc-123",
                    "--subnet-ids",
                    "subnet-a,subnet-b",
                    "-C",
                    str(tmp_path),
                ],
            )
            assert result.exit_code == 0, result.output
            mock_select_name.assert_called_once()

    def test_interactive_fallback_for_vpc_and_subnets(self, runner, tmp_path):
        """Deploy calls _select_vpc and _select_subnets when not provided."""
        with (
            patch("boto3.client") as mock_client,
            patch("zae_limiter.load.cli._select_vpc", return_value="vpc-123") as mock_select_vpc,
            patch(
                "zae_limiter.load.cli._select_subnets", return_value="subnet-a,subnet-b"
            ) as mock_select_subnets,
            patch("zae_limiter.load.builder.build_and_push_locust_image") as mock_build,
            patch("zae_limiter.load.lambda_builder.build_load_lambda_package") as mock_lambda_pkg,
            patch("zae_limiter.load.builder.get_zae_limiter_source") as mock_source,
        ):
            mock_cfn, mock_lambda_client, client_factory = self._deploy_base_mocks()
            mock_client.side_effect = client_factory

            mock_source.return_value = "0.8.0"
            mock_build.return_value = "123.dkr.ecr.us-east-1.amazonaws.com/test:latest"
            zip_path = tmp_path / "lambda.zip"
            zip_path.write_bytes(b"fake zip")
            mock_lambda_pkg.return_value = zip_path

            result = runner.invoke(
                load,
                [
                    "deploy",
                    "--name",
                    "my-app",
                    "-C",
                    str(tmp_path),
                ],
            )
            assert result.exit_code == 0, result.output
            mock_select_vpc.assert_called_once()
            mock_select_subnets.assert_called_once()

    def test_shows_permission_boundary_and_role_format(self, runner, tmp_path):
        """Deploy shows permission boundary and role format from stack outputs."""
        with (
            patch("boto3.client") as mock_client,
            patch("zae_limiter.load.builder.build_and_push_locust_image") as mock_build,
            patch("zae_limiter.load.lambda_builder.build_load_lambda_package") as mock_lambda_pkg,
            patch("zae_limiter.load.builder.get_zae_limiter_source") as mock_source,
        ):
            mock_cfn, mock_lambda_client, client_factory = self._deploy_base_mocks()
            mock_client.side_effect = client_factory

            # Add permission boundary and role format outputs
            mock_cfn.describe_stacks.return_value = {
                "Stacks": [
                    {
                        "Outputs": [
                            {
                                "OutputKey": "AppPolicyArn",
                                "OutputValue": "arn:aws:iam::123:policy/app",
                            },
                            {
                                "OutputKey": "AdminPolicyArn",
                                "OutputValue": "arn:aws:iam::123:policy/admin",
                            },
                            {
                                "OutputKey": "PermissionBoundaryArn",
                                "OutputValue": "arn:aws:iam::aws:policy/PB",
                            },
                            {"OutputKey": "RoleNameFormat", "OutputValue": "PB-{}"},
                        ]
                    }
                ]
            }

            mock_source.return_value = "0.8.0"
            mock_build.return_value = "123.dkr.ecr.us-east-1.amazonaws.com/test:latest"
            zip_path = tmp_path / "lambda.zip"
            zip_path.write_bytes(b"fake zip")
            mock_lambda_pkg.return_value = zip_path

            result = runner.invoke(
                load,
                [
                    "deploy",
                    "--name",
                    "my-app",
                    "--vpc-id",
                    "vpc-123",
                    "--subnet-ids",
                    "subnet-a,subnet-b",
                    "-C",
                    str(tmp_path),
                ],
            )
            assert result.exit_code == 0, result.output
            assert "permission boundary" in result.output.lower()
            assert "role name format" in result.output.lower()

    def test_updates_existing_stack(self, runner, tmp_path):
        """Deploy updates stack when it already exists."""
        with (
            patch("boto3.client") as mock_client,
            patch("zae_limiter.load.builder.build_and_push_locust_image") as mock_build,
            patch("zae_limiter.load.lambda_builder.build_load_lambda_package") as mock_lambda_pkg,
            patch("zae_limiter.load.builder.get_zae_limiter_source") as mock_source,
        ):
            mock_cfn, mock_lambda_client, client_factory = self._deploy_base_mocks()
            mock_client.side_effect = client_factory

            # create_stack raises AlreadyExistsException
            mock_cfn.create_stack.side_effect = mock_cfn.exceptions.AlreadyExistsException("exists")

            mock_source.return_value = "0.8.0"
            mock_build.return_value = "123.dkr.ecr.us-east-1.amazonaws.com/test:latest"
            zip_path = tmp_path / "lambda.zip"
            zip_path.write_bytes(b"fake zip")
            mock_lambda_pkg.return_value = zip_path

            result = runner.invoke(
                load,
                [
                    "deploy",
                    "--name",
                    "my-app",
                    "--vpc-id",
                    "vpc-123",
                    "--subnet-ids",
                    "subnet-a,subnet-b",
                    "-C",
                    str(tmp_path),
                ],
            )
            assert result.exit_code == 0, result.output
            assert "already exists" in result.output.lower()
            mock_cfn.update_stack.assert_called_once()

    def test_no_updates_needed(self, runner, tmp_path):
        """Deploy handles 'No updates are to be performed' gracefully."""
        with (
            patch("boto3.client") as mock_client,
            patch("zae_limiter.load.builder.build_and_push_locust_image") as mock_build,
            patch("zae_limiter.load.lambda_builder.build_load_lambda_package") as mock_lambda_pkg,
            patch("zae_limiter.load.builder.get_zae_limiter_source") as mock_source,
        ):
            mock_cfn, mock_lambda_client, client_factory = self._deploy_base_mocks()
            mock_client.side_effect = client_factory

            mock_cfn.create_stack.side_effect = mock_cfn.exceptions.AlreadyExistsException("exists")
            mock_cfn.update_stack.side_effect = Exception("No updates are to be performed")

            mock_source.return_value = "0.8.0"
            mock_build.return_value = "123.dkr.ecr.us-east-1.amazonaws.com/test:latest"
            zip_path = tmp_path / "lambda.zip"
            zip_path.write_bytes(b"fake zip")
            mock_lambda_pkg.return_value = zip_path

            result = runner.invoke(
                load,
                [
                    "deploy",
                    "--name",
                    "my-app",
                    "--vpc-id",
                    "vpc-123",
                    "--subnet-ids",
                    "subnet-a,subnet-b",
                    "-C",
                    str(tmp_path),
                ],
            )
            assert result.exit_code == 0, result.output
            assert "up to date" in result.output.lower()

    def test_update_reraises_other_errors(self, runner, tmp_path):
        """Deploy re-raises non-'no updates' errors during update."""
        with (
            patch("boto3.client") as mock_client,
            patch("zae_limiter.load.builder.get_zae_limiter_source") as mock_source,
        ):
            mock_cfn, mock_lambda_client, client_factory = self._deploy_base_mocks()
            mock_client.side_effect = client_factory

            mock_cfn.create_stack.side_effect = mock_cfn.exceptions.AlreadyExistsException("exists")
            mock_cfn.update_stack.side_effect = Exception("Access denied")

            mock_source.return_value = "0.8.0"

            result = runner.invoke(
                load,
                [
                    "deploy",
                    "--name",
                    "my-app",
                    "--vpc-id",
                    "vpc-123",
                    "--subnet-ids",
                    "subnet-a,subnet-b",
                    "-C",
                    str(tmp_path),
                ],
            )
            assert result.exit_code != 0


class TestSetupCommand:
    """Tests for the setup command."""

    def test_creates_entities_and_limits(self, runner):
        """Setup creates system defaults, resource configs, and entities."""
        mock_limiter = MagicMock()
        mock_limiter.__aenter__ = AsyncMock(return_value=mock_limiter)
        mock_limiter.__aexit__ = AsyncMock(return_value=False)
        mock_limiter.set_system_defaults = AsyncMock()
        mock_limiter.set_resource_defaults = AsyncMock()
        mock_limiter.create_entity = AsyncMock()
        mock_limiter.set_limits = AsyncMock()

        with patch("zae_limiter.RateLimiter", return_value=mock_limiter):
            result = runner.invoke(
                load,
                [
                    "setup",
                    "--name",
                    "my-app",
                    "--region",
                    "us-east-1",
                    "--custom-limits",
                    "2",
                    "--apis",
                    "2",
                ],
            )
            assert result.exit_code == 0, result.output
            assert "Ready for testing" in result.output


class TestTeardownCommand:
    """Tests for the teardown command."""

    def test_deletes_stack(self, runner):
        """Teardown deletes the load test stack."""
        with patch("boto3.client") as mock_client:
            mock_cfn = MagicMock()
            mock_ecs = MagicMock()

            def client_factory(service, **kwargs):
                if service == "cloudformation":
                    return mock_cfn
                elif service == "ecs":
                    return mock_ecs
                return MagicMock()

            mock_client.side_effect = client_factory

            result = runner.invoke(
                load,
                ["teardown", "--name", "my-app", "--yes"],
            )
            assert result.exit_code == 0
            mock_cfn.delete_stack.assert_called_once_with(StackName="my-app-load")
            assert "Stack deleted" in result.output

    def test_confirms_before_delete(self, runner):
        """Teardown asks for confirmation without --yes."""
        with patch("boto3.client"):
            result = runner.invoke(
                load,
                ["teardown", "--name", "my-app"],
                input="n\n",
            )
            assert result.exit_code != 0  # Aborted

    def test_ecs_stop_exception_ignored(self, runner):
        """Teardown ignores ECS stop errors."""
        with patch("boto3.client") as mock_client:
            mock_cfn = MagicMock()
            mock_ecs = MagicMock()

            def client_factory(service, **kwargs):
                if service == "cloudformation":
                    return mock_cfn
                elif service == "ecs":
                    return mock_ecs
                return MagicMock()

            mock_client.side_effect = client_factory
            mock_ecs.update_service.side_effect = Exception("service not found")

            result = runner.invoke(
                load,
                ["teardown", "--name", "my-app", "--yes"],
            )
            assert result.exit_code == 0
            assert "Stack deleted" in result.output


class TestConnectCommand:
    """Tests for the connect command."""

    def _make_task_response(
        self,
        *,
        status="RUNNING",
        runtime_id="runtime-123",
        ssm_status="RUNNING",
    ):
        """Build a describe_tasks response."""
        container = {"name": "locust-master"}
        if runtime_id:
            container["runtimeId"] = runtime_id
        if ssm_status:
            container["managedAgents"] = [{"name": "ExecuteCommandAgent", "lastStatus": ssm_status}]
        else:
            container["managedAgents"] = []

        return {
            "tasks": [
                {
                    "lastStatus": status,
                    "containers": [container],
                }
            ]
        }

    def test_task_already_running(self, runner):
        """Connect reuses existing running task."""
        with (
            patch("boto3.client") as mock_client,
            patch("subprocess.run") as mock_subprocess_run,
            patch("time.sleep"),
        ):
            mock_ecs = MagicMock()
            mock_client.return_value = mock_ecs

            # Task already running on first check
            mock_ecs.list_tasks.return_value = {
                "taskArns": ["arn:aws:ecs:us-east-1:123:task/cluster/task-id"]
            }
            mock_ecs.describe_tasks.return_value = self._make_task_response()
            mock_subprocess_run.return_value = MagicMock(returncode=0)

            result = runner.invoke(
                load,
                ["connect", "--name", "my-app", "--destroy"],
            )
            assert result.exit_code == 0, result.output
            assert "Found running Fargate task" in result.output

    def test_task_not_running_status(self, runner):
        """Connect handles task that exists but isn't RUNNING."""
        with (
            patch("boto3.client") as mock_client,
            patch("subprocess.run") as mock_subprocess_run,
            patch("time.sleep"),
        ):
            mock_ecs = MagicMock()
            mock_client.return_value = mock_ecs

            pending_response = self._make_task_response(status="PENDING")
            running_response = self._make_task_response()

            mock_ecs.list_tasks.side_effect = [
                {"taskArns": ["arn:aws:ecs:us-east-1:123:task/cluster/task-id"]},  # initial
                {"taskArns": ["arn:aws:ecs:us-east-1:123:task/cluster/task-id"]},  # poll
                {"taskArns": ["arn:aws:ecs:us-east-1:123:task/cluster/task-id"]},  # ssm
            ]
            mock_ecs.describe_tasks.side_effect = [
                pending_response,  # initial check - PENDING
                running_response,  # poll - RUNNING
                running_response,  # ssm check
            ]
            mock_subprocess_run.return_value = MagicMock(returncode=0)

            result = runner.invoke(
                load,
                ["connect", "--name", "my-app", "--destroy"],
            )
            assert result.exit_code == 0, result.output

    def test_task_no_runtime_id(self, runner):
        """Connect handles task without runtime ID."""
        with (
            patch("boto3.client") as mock_client,
            patch("subprocess.run") as mock_subprocess_run,
            patch("time.sleep"),
        ):
            mock_ecs = MagicMock()
            mock_client.return_value = mock_ecs

            no_runtime = self._make_task_response(runtime_id=None)
            with_runtime = self._make_task_response()

            mock_ecs.list_tasks.side_effect = [
                {"taskArns": ["arn:aws:ecs:us-east-1:123:task/cluster/task-id"]},
                {"taskArns": ["arn:aws:ecs:us-east-1:123:task/cluster/task-id"]},
                {"taskArns": ["arn:aws:ecs:us-east-1:123:task/cluster/task-id"]},
            ]
            mock_ecs.describe_tasks.side_effect = [
                no_runtime,
                with_runtime,
                with_runtime,
            ]
            mock_subprocess_run.return_value = MagicMock(returncode=0)

            result = runner.invoke(
                load,
                ["connect", "--name", "my-app", "--destroy"],
            )
            assert result.exit_code == 0, result.output

    def test_ssm_not_ready_waits(self, runner):
        """Connect waits for SSM agent to become ready."""
        with (
            patch("boto3.client") as mock_client,
            patch("subprocess.run") as mock_subprocess_run,
            patch("time.sleep"),
        ):
            mock_ecs = MagicMock()
            mock_client.return_value = mock_ecs

            no_ssm = self._make_task_response(ssm_status="PENDING")
            with_ssm = self._make_task_response()

            mock_ecs.list_tasks.return_value = {
                "taskArns": ["arn:aws:ecs:us-east-1:123:task/cluster/task-id"]
            }
            mock_ecs.describe_tasks.side_effect = [
                with_ssm,  # initial check
                no_ssm,  # ssm poll 1 - not ready
                with_ssm,  # ssm poll 2 - ready
            ]
            mock_subprocess_run.return_value = MagicMock(returncode=0)

            result = runner.invoke(
                load,
                ["connect", "--name", "my-app", "--destroy"],
            )
            assert result.exit_code == 0, result.output

    def test_task_start_timeout(self, runner):
        """Connect exits when task fails to start within timeout."""
        with (
            patch("boto3.client") as mock_client,
            patch("time.sleep"),
        ):
            mock_ecs = MagicMock()
            mock_client.return_value = mock_ecs

            # No tasks ever appear
            mock_ecs.list_tasks.return_value = {"taskArns": []}

            result = runner.invoke(
                load,
                ["connect", "--name", "my-app"],
            )
            assert result.exit_code != 0
            assert "failed to start" in result.output.lower()

    def test_ssm_agent_timeout(self, runner):
        """Connect exits when SSM agent fails to start."""
        with (
            patch("boto3.client") as mock_client,
            patch("time.sleep"),
        ):
            mock_ecs = MagicMock()
            mock_client.return_value = mock_ecs

            running_no_ssm = self._make_task_response(ssm_status="PENDING")

            mock_ecs.list_tasks.return_value = {
                "taskArns": ["arn:aws:ecs:us-east-1:123:task/cluster/task-id"]
            }
            # Always return SSM not ready
            mock_ecs.describe_tasks.return_value = running_no_ssm

            result = runner.invoke(
                load,
                ["connect", "--name", "my-app"],
            )
            assert result.exit_code != 0
            assert "ssm agent failed" in result.output.lower()

    def test_ssm_retry_on_failure(self, runner):
        """Connect retries SSM connection on failure."""
        with (
            patch("boto3.client") as mock_client,
            patch("subprocess.run") as mock_subprocess_run,
            patch("time.sleep"),
        ):
            mock_ecs = MagicMock()
            mock_client.return_value = mock_ecs

            mock_ecs.list_tasks.return_value = {
                "taskArns": ["arn:aws:ecs:us-east-1:123:task/cluster/task-id"]
            }
            mock_ecs.describe_tasks.return_value = self._make_task_response()

            # Fail first two attempts, succeed on third
            mock_subprocess_run.side_effect = [
                MagicMock(returncode=1),
                MagicMock(returncode=1),
                MagicMock(returncode=0),
            ]

            result = runner.invoke(
                load,
                ["connect", "--name", "my-app", "--destroy"],
            )
            assert result.exit_code == 0, result.output
            assert mock_subprocess_run.call_count == 3
            assert "retrying" in result.output.lower()

    def test_ssm_all_retries_fail(self, runner):
        """Connect reports failure after all SSM retries exhausted."""
        with (
            patch("boto3.client") as mock_client,
            patch("subprocess.run") as mock_subprocess_run,
            patch("time.sleep"),
        ):
            mock_ecs = MagicMock()
            mock_client.return_value = mock_ecs

            mock_ecs.list_tasks.return_value = {
                "taskArns": ["arn:aws:ecs:us-east-1:123:task/cluster/task-id"]
            }
            mock_ecs.describe_tasks.return_value = self._make_task_response()

            # All 5 attempts fail
            mock_subprocess_run.return_value = MagicMock(returncode=1)

            result = runner.invoke(
                load,
                ["connect", "--name", "my-app", "--destroy"],
            )
            assert mock_subprocess_run.call_count == 5
            assert "failed after 5 attempts" in result.output.lower()

    def test_keyboard_interrupt_during_connect(self, runner):
        """Connect handles KeyboardInterrupt gracefully."""
        with (
            patch("boto3.client") as mock_client,
            patch("subprocess.run") as mock_subprocess_run,
            patch("time.sleep"),
        ):
            mock_ecs = MagicMock()
            mock_client.return_value = mock_ecs

            mock_ecs.list_tasks.return_value = {
                "taskArns": ["arn:aws:ecs:us-east-1:123:task/cluster/task-id"]
            }
            mock_ecs.describe_tasks.return_value = self._make_task_response()
            mock_subprocess_run.side_effect = KeyboardInterrupt

            result = runner.invoke(
                load,
                ["connect", "--name", "my-app", "--destroy"],
            )
            assert "Interrupted" in result.output

    def test_stop_task_exception_in_finally(self, runner):
        """Connect handles exceptions when stopping task in finally."""
        with (
            patch("boto3.client") as mock_client,
            patch("subprocess.run") as mock_subprocess_run,
            patch("time.sleep"),
        ):
            mock_ecs = MagicMock()
            mock_client.return_value = mock_ecs

            mock_ecs.list_tasks.return_value = {
                "taskArns": ["arn:aws:ecs:us-east-1:123:task/cluster/task-id"]
            }
            mock_ecs.describe_tasks.return_value = self._make_task_response()
            mock_subprocess_run.return_value = MagicMock(returncode=0)

            # Make stop_task fail
            mock_ecs.stop_task.side_effect = Exception("cannot stop task")

            result = runner.invoke(
                load,
                ["connect", "--name", "my-app", "--destroy"],
            )
            assert "Warning" in result.output or "cannot stop" in result.output.lower()

    def test_no_destroy_keeps_task_running(self, runner):
        """Connect without --destroy keeps task running after disconnect."""
        with (
            patch("boto3.client") as mock_client,
            patch("subprocess.run") as mock_subprocess_run,
            patch("time.sleep"),
        ):
            mock_ecs = MagicMock()
            mock_client.return_value = mock_ecs

            # Task already running
            mock_ecs.list_tasks.return_value = {
                "taskArns": ["arn:aws:ecs:us-east-1:123:task/cluster/task-id"]
            }
            mock_ecs.describe_tasks.return_value = self._make_task_response()
            mock_subprocess_run.return_value = MagicMock(returncode=0)

            result = runner.invoke(
                load,
                ["connect", "--name", "my-app"],  # no --destroy
            )
            assert result.exit_code == 0, result.output
            assert "still running" in result.output.lower()

    def test_starts_fargate_and_connects(self, runner):
        """Connect starts task with run_task and SSM tunnel."""
        with (
            patch("boto3.client") as mock_client,
            patch("subprocess.run") as mock_subprocess_run,
            patch("time.sleep"),
        ):
            mock_ecs = MagicMock()
            mock_client.return_value = mock_ecs

            # First call: no tasks, second: task running
            mock_ecs.list_tasks.side_effect = [
                {"taskArns": []},  # initial check
                {"taskArns": ["arn:aws:ecs:us-east-1:123:task/cluster/task-id"]},  # poll
                {"taskArns": ["arn:aws:ecs:us-east-1:123:task/cluster/task-id"]},  # ssm check
            ]
            mock_ecs.describe_tasks.return_value = {
                "tasks": [
                    {
                        "lastStatus": "RUNNING",
                        "containers": [
                            {
                                "name": "locust-master",
                                "runtimeId": "runtime-123",
                                "managedAgents": [
                                    {
                                        "name": "ExecuteCommandAgent",
                                        "lastStatus": "RUNNING",
                                    }
                                ],
                            }
                        ],
                    }
                ]
            }
            mock_ecs.describe_services.return_value = {
                "services": [
                    {
                        "networkConfiguration": {
                            "awsvpcConfiguration": {
                                "subnets": ["subnet-a"],
                                "securityGroups": ["sg-123"],
                                "assignPublicIp": "DISABLED",
                            }
                        }
                    }
                ]
            }
            mock_ecs.run_task.return_value = {
                "tasks": [{"taskArn": "arn:aws:ecs:us-east-1:123:task/cluster/task-id"}]
            }

            mock_subprocess_run.return_value = MagicMock(returncode=0)

            result = runner.invoke(
                load,
                ["connect", "--name", "my-app", "--destroy"],
            )
            assert result.exit_code == 0, result.output
            # Should have called run_task
            mock_ecs.run_task.assert_called_once()

    def test_uses_run_task_with_overrides(self, runner):
        """Connect uses run_task with overrides instead of service scaling."""
        with (
            patch("boto3.client") as mock_client,
            patch("subprocess.run") as mock_subprocess_run,
            patch("time.sleep"),
        ):
            mock_ecs = MagicMock()
            mock_client.return_value = mock_ecs

            # No tasks running initially
            mock_ecs.list_tasks.side_effect = [
                {"taskArns": []},  # initial check
                {"taskArns": ["arn:aws:ecs:us-east-1:123:task/cluster/task-id"]},  # after run_task
                {"taskArns": ["arn:aws:ecs:us-east-1:123:task/cluster/task-id"]},  # ssm check
            ]
            mock_ecs.describe_tasks.return_value = self._make_task_response()
            mock_ecs.describe_services.return_value = {
                "services": [
                    {
                        "networkConfiguration": {
                            "awsvpcConfiguration": {
                                "subnets": ["subnet-a"],
                                "securityGroups": ["sg-123"],
                                "assignPublicIp": "DISABLED",
                            }
                        }
                    }
                ]
            }
            mock_ecs.run_task.return_value = {
                "tasks": [{"taskArn": "arn:aws:ecs:us-east-1:123:task/cluster/task-id"}]
            }
            mock_subprocess_run.return_value = MagicMock(returncode=0)

            result = runner.invoke(
                load,
                ["connect", "--name", "my-app", "--max-workers", "50", "--destroy"],
            )
            assert result.exit_code == 0, result.output

            # Should have called run_task, not update_service for scaling up
            mock_ecs.run_task.assert_called_once()
            call_kwargs = mock_ecs.run_task.call_args[1]
            assert call_kwargs["cluster"] == "my-app-load"
            assert "overrides" in call_kwargs

            # Should use stop_task on cleanup, not update_service
            mock_ecs.stop_task.assert_called()

    def test_force_restarts_running_task(self, runner):
        """Connect --force stops existing task and starts new one."""
        with (
            patch("boto3.client") as mock_client,
            patch("subprocess.run") as mock_subprocess_run,
            patch("time.sleep"),
        ):
            mock_ecs = MagicMock()
            mock_client.return_value = mock_ecs

            # Task already running
            mock_ecs.list_tasks.side_effect = [
                {"taskArns": ["arn:aws:ecs:us-east-1:123:task/cluster/old-task"]},  # initial
                {"taskArns": []},  # after stop
                {"taskArns": ["arn:aws:ecs:us-east-1:123:task/cluster/new-task"]},  # after run
                {"taskArns": ["arn:aws:ecs:us-east-1:123:task/cluster/new-task"]},  # ssm
            ]
            mock_ecs.describe_tasks.return_value = self._make_task_response()
            mock_ecs.describe_services.return_value = {
                "services": [
                    {
                        "networkConfiguration": {
                            "awsvpcConfiguration": {
                                "subnets": ["subnet-a"],
                                "securityGroups": ["sg-123"],
                                "assignPublicIp": "DISABLED",
                            }
                        }
                    }
                ]
            }
            mock_ecs.run_task.return_value = {
                "tasks": [{"taskArn": "arn:aws:ecs:us-east-1:123:task/cluster/new-task"}]
            }
            mock_subprocess_run.return_value = MagicMock(returncode=0)

            result = runner.invoke(
                load,
                ["connect", "--name", "my-app", "--max-workers", "50", "--force", "--destroy"],
            )
            assert result.exit_code == 0, result.output

            # Should have stopped the old task
            mock_ecs.stop_task.assert_any_call(
                cluster="my-app-load",
                task="arn:aws:ecs:us-east-1:123:task/cluster/old-task",
            )

    def test_warns_when_task_running_with_overrides_no_force(self, runner):
        """Connect warns when task exists and overrides provided without --force."""
        with (
            patch("boto3.client") as mock_client,
            patch("subprocess.run") as mock_subprocess_run,
            patch("time.sleep"),
        ):
            mock_ecs = MagicMock()
            mock_client.return_value = mock_ecs

            # Task already running
            mock_ecs.list_tasks.return_value = {
                "taskArns": ["arn:aws:ecs:us-east-1:123:task/cluster/task-id"]
            }
            mock_ecs.describe_tasks.return_value = self._make_task_response()
            mock_subprocess_run.return_value = MagicMock(returncode=0)

            result = runner.invoke(
                load,
                ["connect", "--name", "my-app", "--max-workers", "50", "--destroy"],
            )
            assert result.exit_code == 0, result.output
            assert "already running" in result.output.lower()
            assert "--force" in result.output

    def test_standalone_mode_overrides_command(self, runner):
        """Connect --standalone overrides master command."""
        with (
            patch("boto3.client") as mock_client,
            patch("subprocess.run") as mock_subprocess_run,
            patch("time.sleep"),
        ):
            mock_ecs = MagicMock()
            mock_client.return_value = mock_ecs

            mock_ecs.list_tasks.side_effect = [
                {"taskArns": []},
                {"taskArns": ["arn:aws:ecs:us-east-1:123:task/cluster/task-id"]},
                {"taskArns": ["arn:aws:ecs:us-east-1:123:task/cluster/task-id"]},
            ]
            mock_ecs.describe_tasks.return_value = self._make_task_response()
            mock_ecs.describe_services.return_value = {
                "services": [
                    {
                        "networkConfiguration": {
                            "awsvpcConfiguration": {
                                "subnets": ["subnet-a"],
                                "securityGroups": ["sg-123"],
                                "assignPublicIp": "DISABLED",
                            }
                        }
                    }
                ]
            }
            mock_ecs.run_task.return_value = {
                "tasks": [{"taskArn": "arn:aws:ecs:us-east-1:123:task/cluster/task-id"}]
            }
            mock_subprocess_run.return_value = MagicMock(returncode=0)

            result = runner.invoke(
                load,
                ["connect", "--name", "my-app", "--standalone", "--destroy"],
            )
            assert result.exit_code == 0, result.output

            # Verify overrides include standalone command
            call_kwargs = mock_ecs.run_task.call_args[1]
            overrides = call_kwargs["overrides"]
            master = next(
                c for c in overrides["containerOverrides"] if c["name"] == "locust-master"
            )
            assert "command" in master


class TestBuildTaskOverrides:
    """Tests for _build_task_overrides helper."""

    def test_empty_overrides_returns_empty_dict(self):
        from zae_limiter.load.cli import _build_task_overrides

        result = _build_task_overrides(
            standalone=False,
            locustfile=None,
            max_workers=None,
            desired_workers=None,
            min_workers=None,
            users_per_worker=None,
            rps_per_worker=None,
            startup_lead_time=None,
        )
        assert result == {}

    def test_scaling_params_override_orchestrator_env(self):
        from zae_limiter.load.cli import _build_task_overrides

        result = _build_task_overrides(
            standalone=False,
            locustfile=None,
            max_workers=50,
            desired_workers=10,
            min_workers=2,
            users_per_worker=25,
            rps_per_worker=100,
            startup_lead_time=30,
        )

        orchestrator = next(
            c for c in result["containerOverrides"] if c["name"] == "worker-orchestrator"
        )
        env_dict = {e["name"]: e["value"] for e in orchestrator["environment"]}

        assert env_dict["MAX_WORKERS"] == "50"
        assert env_dict["DESIRED_WORKERS"] == "10"
        assert env_dict["MIN_WORKERS"] == "2"
        assert env_dict["USERS_PER_WORKER"] == "25"
        assert env_dict["RPS_PER_WORKER"] == "100"
        assert env_dict["STARTUP_LEAD_TIME"] == "30"

    def test_locustfile_overrides_both_containers(self):
        from zae_limiter.load.cli import _build_task_overrides

        result = _build_task_overrides(
            standalone=False,
            locustfile="locustfiles/llm_production.py",
            max_workers=None,
            desired_workers=None,
            min_workers=None,
            users_per_worker=None,
            rps_per_worker=None,
            startup_lead_time=None,
        )

        for container_name in ["locust-master", "worker-orchestrator"]:
            container = next(c for c in result["containerOverrides"] if c["name"] == container_name)
            env_dict = {e["name"]: e["value"] for e in container["environment"]}
            assert env_dict["LOCUSTFILE"] == "locustfiles/llm_production.py"

    def test_standalone_mode_overrides_master_command(self):
        from zae_limiter.load.cli import _build_task_overrides

        result = _build_task_overrides(
            standalone=True,
            locustfile="simple.py",
            max_workers=None,
            desired_workers=None,
            min_workers=None,
            users_per_worker=None,
            rps_per_worker=None,
            startup_lead_time=None,
        )

        master = next(c for c in result["containerOverrides"] if c["name"] == "locust-master")
        assert "command" in master
        assert "--master" not in " ".join(master["command"])
        assert "simple.py" in " ".join(master["command"])

    def test_standalone_mode_sets_orchestrator_idle(self):
        from zae_limiter.load.cli import _build_task_overrides

        result = _build_task_overrides(
            standalone=True,
            locustfile=None,
            max_workers=None,
            desired_workers=None,
            min_workers=None,
            users_per_worker=None,
            rps_per_worker=None,
            startup_lead_time=None,
        )

        orchestrator = next(
            c for c in result["containerOverrides"] if c["name"] == "worker-orchestrator"
        )
        env_dict = {e["name"]: e["value"] for e in orchestrator["environment"]}
        assert env_dict["DESIRED_WORKERS"] == "0"
        assert env_dict["MIN_WORKERS"] == "0"


class TestGetServiceNetworkConfig:
    """Tests for _get_service_network_config helper."""

    def test_extracts_network_config_from_service(self):
        from zae_limiter.load.cli import _get_service_network_config

        mock_ecs = MagicMock()
        mock_ecs.describe_services.return_value = {
            "services": [
                {
                    "networkConfiguration": {
                        "awsvpcConfiguration": {
                            "subnets": ["subnet-a", "subnet-b"],
                            "securityGroups": ["sg-123"],
                            "assignPublicIp": "DISABLED",
                        }
                    }
                }
            ]
        }

        result = _get_service_network_config(mock_ecs, "my-cluster", "my-service")

        assert result == {
            "awsvpcConfiguration": {
                "subnets": ["subnet-a", "subnet-b"],
                "securityGroups": ["sg-123"],
                "assignPublicIp": "DISABLED",
            }
        }
        mock_ecs.describe_services.assert_called_once_with(
            cluster="my-cluster",
            services=["my-service"],
        )
