"""Tests for load test CLI commands."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import click
import pytest
from click.testing import CliRunner

from zae_limiter.loadtest.cli import loadtest


@pytest.fixture()
def runner():
    return CliRunner()


# ---------------------------------------------------------------------------
# Interactive selection helpers
# ---------------------------------------------------------------------------


class TestSelectName:
    """Tests for _select_name interactive selector."""

    def test_returns_selected_stack(self):
        from zae_limiter.loadtest.cli import _select_name

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
        from zae_limiter.loadtest.cli import _select_name

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
        from zae_limiter.loadtest.cli import _select_vpc

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
        from zae_limiter.loadtest.cli import _select_vpc

        with patch("boto3.client") as mock_client:
            mock_ec2 = MagicMock()
            mock_client.return_value = mock_ec2
            mock_ec2.describe_vpcs.return_value = {"Vpcs": []}
            with pytest.raises(SystemExit):
                _select_vpc("us-east-1")

    def test_vpc_without_name_tag(self):
        from zae_limiter.loadtest.cli import _select_vpc

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
        from zae_limiter.loadtest.cli import _select_subnets

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
        from zae_limiter.loadtest.cli import _select_subnets

        with patch("boto3.client") as mock_client:
            mock_ec2 = MagicMock()
            mock_client.return_value = mock_ec2
            mock_ec2.describe_subnets.return_value = {"Subnets": []}
            with pytest.raises(SystemExit):
                _select_subnets("us-east-1", "vpc-123")

    def test_exits_when_fewer_than_two_selected(self):
        from zae_limiter.loadtest.cli import _select_subnets

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
                            "OutputKey": "AcquireOnlyPolicyArn",
                            "OutputValue": "arn:aws:iam::123:policy/acq",
                        },
                        {
                            "OutputKey": "FullAccessPolicyArn",
                            "OutputValue": "arn:aws:iam::123:policy/full",
                        },
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
                loadtest,
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
                loadtest,
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
            patch("zae_limiter.loadtest.builder.build_and_push_locust_image") as mock_build,
            patch(
                "zae_limiter.loadtest.lambda_builder.build_load_lambda_package"
            ) as mock_lambda_pkg,
            patch("zae_limiter.loadtest.builder.get_zae_limiter_source") as mock_source,
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
                                "OutputKey": "AcquireOnlyPolicyArn",
                                "OutputValue": "arn:aws:iam::123:policy/acq",
                            },
                            {
                                "OutputKey": "FullAccessPolicyArn",
                                "OutputValue": "arn:aws:iam::123:policy/full",
                            },
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
                loadtest,
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
            patch(
                "zae_limiter.loadtest.cli._select_name", return_value="my-app"
            ) as mock_select_name,
            patch("zae_limiter.loadtest.builder.build_and_push_locust_image") as mock_build,
            patch(
                "zae_limiter.loadtest.lambda_builder.build_load_lambda_package"
            ) as mock_lambda_pkg,
            patch("zae_limiter.loadtest.builder.get_zae_limiter_source") as mock_source,
        ):
            mock_cfn, mock_lambda_client, client_factory = self._deploy_base_mocks()
            mock_client.side_effect = client_factory

            mock_source.return_value = "0.8.0"
            mock_build.return_value = "123.dkr.ecr.us-east-1.amazonaws.com/test:latest"
            zip_path = tmp_path / "lambda.zip"
            zip_path.write_bytes(b"fake zip")
            mock_lambda_pkg.return_value = zip_path

            result = runner.invoke(
                loadtest,
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
            patch(
                "zae_limiter.loadtest.cli._select_vpc", return_value="vpc-123"
            ) as mock_select_vpc,
            patch(
                "zae_limiter.loadtest.cli._select_subnets", return_value="subnet-a,subnet-b"
            ) as mock_select_subnets,
            patch("zae_limiter.loadtest.builder.build_and_push_locust_image") as mock_build,
            patch(
                "zae_limiter.loadtest.lambda_builder.build_load_lambda_package"
            ) as mock_lambda_pkg,
            patch("zae_limiter.loadtest.builder.get_zae_limiter_source") as mock_source,
        ):
            mock_cfn, mock_lambda_client, client_factory = self._deploy_base_mocks()
            mock_client.side_effect = client_factory

            mock_source.return_value = "0.8.0"
            mock_build.return_value = "123.dkr.ecr.us-east-1.amazonaws.com/test:latest"
            zip_path = tmp_path / "lambda.zip"
            zip_path.write_bytes(b"fake zip")
            mock_lambda_pkg.return_value = zip_path

            result = runner.invoke(
                loadtest,
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
            patch("zae_limiter.loadtest.builder.build_and_push_locust_image") as mock_build,
            patch(
                "zae_limiter.loadtest.lambda_builder.build_load_lambda_package"
            ) as mock_lambda_pkg,
            patch("zae_limiter.loadtest.builder.get_zae_limiter_source") as mock_source,
        ):
            mock_cfn, mock_lambda_client, client_factory = self._deploy_base_mocks()
            mock_client.side_effect = client_factory

            # Add permission boundary and role format outputs
            mock_cfn.describe_stacks.return_value = {
                "Stacks": [
                    {
                        "Outputs": [
                            {
                                "OutputKey": "AcquireOnlyPolicyArn",
                                "OutputValue": "arn:aws:iam::123:policy/acq",
                            },
                            {
                                "OutputKey": "FullAccessPolicyArn",
                                "OutputValue": "arn:aws:iam::123:policy/full",
                            },
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
                loadtest,
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
            patch("zae_limiter.loadtest.builder.build_and_push_locust_image") as mock_build,
            patch(
                "zae_limiter.loadtest.lambda_builder.build_load_lambda_package"
            ) as mock_lambda_pkg,
            patch("zae_limiter.loadtest.builder.get_zae_limiter_source") as mock_source,
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
                loadtest,
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
            patch("zae_limiter.loadtest.builder.build_and_push_locust_image") as mock_build,
            patch(
                "zae_limiter.loadtest.lambda_builder.build_load_lambda_package"
            ) as mock_lambda_pkg,
            patch("zae_limiter.loadtest.builder.get_zae_limiter_source") as mock_source,
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
                loadtest,
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

    def test_capacity_provider_passed_to_cloudformation(self, runner, tmp_path):
        """Deploy passes --capacity-provider to CloudFormation parameters."""
        with (
            patch("boto3.client") as mock_client,
            patch("zae_limiter.loadtest.builder.build_and_push_locust_image") as mock_build,
            patch(
                "zae_limiter.loadtest.lambda_builder.build_load_lambda_package"
            ) as mock_lambda_pkg,
            patch("zae_limiter.loadtest.builder.get_zae_limiter_source") as mock_source,
        ):
            mock_cfn, mock_lambda_client, client_factory = self._deploy_base_mocks()
            mock_client.side_effect = client_factory

            mock_source.return_value = "0.8.0"
            mock_build.return_value = "123.dkr.ecr.us-east-1.amazonaws.com/test:latest"
            zip_path = tmp_path / "lambda.zip"
            zip_path.write_bytes(b"fake zip")
            mock_lambda_pkg.return_value = zip_path

            result = runner.invoke(
                loadtest,
                [
                    "deploy",
                    "--name",
                    "my-app",
                    "--vpc-id",
                    "vpc-123",
                    "--subnet-ids",
                    "subnet-a,subnet-b",
                    "--capacity-provider",
                    "FARGATE",
                    "-C",
                    str(tmp_path),
                ],
            )
            assert result.exit_code == 0, result.output

            # Verify CapacityProvider param was passed to create_stack
            call_kwargs = mock_cfn.create_stack.call_args[1]
            params = {p["ParameterKey"]: p["ParameterValue"] for p in call_kwargs["Parameters"]}
            assert params["CapacityProvider"] == "FARGATE"

    def test_capacity_provider_defaults_to_fargate_spot(self, runner, tmp_path):
        """Deploy defaults --capacity-provider to FARGATE_SPOT."""
        with (
            patch("boto3.client") as mock_client,
            patch("zae_limiter.loadtest.builder.build_and_push_locust_image") as mock_build,
            patch(
                "zae_limiter.loadtest.lambda_builder.build_load_lambda_package"
            ) as mock_lambda_pkg,
            patch("zae_limiter.loadtest.builder.get_zae_limiter_source") as mock_source,
        ):
            mock_cfn, mock_lambda_client, client_factory = self._deploy_base_mocks()
            mock_client.side_effect = client_factory

            mock_source.return_value = "0.8.0"
            mock_build.return_value = "123.dkr.ecr.us-east-1.amazonaws.com/test:latest"
            zip_path = tmp_path / "lambda.zip"
            zip_path.write_bytes(b"fake zip")
            mock_lambda_pkg.return_value = zip_path

            result = runner.invoke(
                loadtest,
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

            call_kwargs = mock_cfn.create_stack.call_args[1]
            params = {p["ParameterKey"]: p["ParameterValue"] for p in call_kwargs["Parameters"]}
            assert params["CapacityProvider"] == "FARGATE_SPOT"

    def test_ssm_endpoint_passed_to_cloudformation(self, runner, tmp_path):
        """Deploy passes --ssm-endpoint to CloudFormation as CreateSsmEndpoint."""
        with (
            patch("boto3.client") as mock_client,
            patch("zae_limiter.loadtest.builder.build_and_push_locust_image") as mock_build,
            patch(
                "zae_limiter.loadtest.lambda_builder.build_load_lambda_package"
            ) as mock_lambda_pkg,
            patch("zae_limiter.loadtest.builder.get_zae_limiter_source") as mock_source,
        ):
            mock_cfn, mock_lambda_client, client_factory = self._deploy_base_mocks()
            mock_client.side_effect = client_factory

            mock_source.return_value = "0.8.0"
            mock_build.return_value = "123.dkr.ecr.us-east-1.amazonaws.com/test:latest"
            zip_path = tmp_path / "lambda.zip"
            zip_path.write_bytes(b"fake zip")
            mock_lambda_pkg.return_value = zip_path

            result = runner.invoke(
                loadtest,
                [
                    "deploy",
                    "--name",
                    "my-app",
                    "--vpc-id",
                    "vpc-123",
                    "--subnet-ids",
                    "subnet-a,subnet-b",
                    "--ssm-endpoint",
                    "-C",
                    str(tmp_path),
                ],
            )
            assert result.exit_code == 0, result.output

            call_kwargs = mock_cfn.create_stack.call_args[1]
            params = {p["ParameterKey"]: p["ParameterValue"] for p in call_kwargs["Parameters"]}
            assert params["CreateSsmEndpoint"] == "true"

    def test_no_dynamodb_endpoint_skips_route_table_discovery(self, runner, tmp_path):
        """Deploy with --no-dynamodb-endpoint skips route table discovery."""
        with (
            patch("boto3.client") as mock_client,
            patch("zae_limiter.loadtest.builder.build_and_push_locust_image") as mock_build,
            patch(
                "zae_limiter.loadtest.lambda_builder.build_load_lambda_package"
            ) as mock_lambda_pkg,
            patch("zae_limiter.loadtest.builder.get_zae_limiter_source") as mock_source,
            patch("zae_limiter.loadtest.cli._discover_route_tables") as mock_discover_rt,
        ):
            mock_cfn, mock_lambda_client, client_factory = self._deploy_base_mocks()
            mock_client.side_effect = client_factory

            mock_source.return_value = "0.8.0"
            mock_build.return_value = "123.dkr.ecr.us-east-1.amazonaws.com/test:latest"
            zip_path = tmp_path / "lambda.zip"
            zip_path.write_bytes(b"fake zip")
            mock_lambda_pkg.return_value = zip_path

            result = runner.invoke(
                loadtest,
                [
                    "deploy",
                    "--name",
                    "my-app",
                    "--vpc-id",
                    "vpc-123",
                    "--subnet-ids",
                    "subnet-a,subnet-b",
                    "--no-dynamodb-endpoint",
                    "-C",
                    str(tmp_path),
                ],
            )
            assert result.exit_code == 0, result.output

            # Route table discovery should not be called
            mock_discover_rt.assert_not_called()

            # PrivateRouteTableIds should be empty
            call_kwargs = mock_cfn.create_stack.call_args[1]
            params = {p["ParameterKey"]: p["ParameterValue"] for p in call_kwargs["Parameters"]}
            assert params["PrivateRouteTableIds"] == ""

    def test_dynamodb_endpoint_skips_when_already_exists(self, runner, tmp_path):
        """Deploy clears route tables when DynamoDB endpoint already exists."""
        with (
            patch("boto3.client") as mock_client,
            patch("zae_limiter.loadtest.builder.build_and_push_locust_image") as mock_build,
            patch(
                "zae_limiter.loadtest.lambda_builder.build_load_lambda_package"
            ) as mock_lambda_pkg,
            patch("zae_limiter.loadtest.builder.get_zae_limiter_source") as mock_source,
        ):
            mock_cfn, mock_lambda_client, client_factory = self._deploy_base_mocks()
            mock_ec2 = MagicMock()
            mock_ec2.meta.region_name = "us-east-1"

            def full_client_factory(service, **kwargs):
                if service == "ec2":
                    return mock_ec2
                return client_factory(service, **kwargs)

            mock_client.side_effect = full_client_factory

            # Route tables found, but endpoint already exists
            mock_ec2.describe_route_tables.return_value = {
                "RouteTables": [{"RouteTableId": "rtb-123"}]
            }
            mock_ec2.describe_vpc_endpoints.return_value = {
                "VpcEndpoints": [{"VpcEndpointId": "vpce-ddb-existing"}]
            }

            mock_source.return_value = "0.8.0"
            mock_build.return_value = "123.dkr.ecr.us-east-1.amazonaws.com/test:latest"
            zip_path = tmp_path / "lambda.zip"
            zip_path.write_bytes(b"fake zip")
            mock_lambda_pkg.return_value = zip_path

            result = runner.invoke(
                loadtest,
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
            assert "already exists" in result.output.lower()

            # PrivateRouteTableIds should be empty (cleared)
            call_kwargs = mock_cfn.create_stack.call_args[1]
            params = {p["ParameterKey"]: p["ParameterValue"] for p in call_kwargs["Parameters"]}
            assert params["PrivateRouteTableIds"] == ""

    def test_dynamodb_endpoint_passes_route_tables_when_no_existing(self, runner, tmp_path):
        """Deploy passes route tables when no DynamoDB endpoint exists."""
        with (
            patch("boto3.client") as mock_client,
            patch("zae_limiter.loadtest.builder.build_and_push_locust_image") as mock_build,
            patch(
                "zae_limiter.loadtest.lambda_builder.build_load_lambda_package"
            ) as mock_lambda_pkg,
            patch("zae_limiter.loadtest.builder.get_zae_limiter_source") as mock_source,
        ):
            mock_cfn, mock_lambda_client, client_factory = self._deploy_base_mocks()
            mock_ec2 = MagicMock()
            mock_ec2.meta.region_name = "us-east-1"

            def full_client_factory(service, **kwargs):
                if service == "ec2":
                    return mock_ec2
                return client_factory(service, **kwargs)

            mock_client.side_effect = full_client_factory

            # Route tables found, no existing endpoint
            mock_ec2.describe_route_tables.return_value = {
                "RouteTables": [{"RouteTableId": "rtb-123"}]
            }
            mock_ec2.describe_vpc_endpoints.return_value = {"VpcEndpoints": []}

            mock_source.return_value = "0.8.0"
            mock_build.return_value = "123.dkr.ecr.us-east-1.amazonaws.com/test:latest"
            zip_path = tmp_path / "lambda.zip"
            zip_path.write_bytes(b"fake zip")
            mock_lambda_pkg.return_value = zip_path

            result = runner.invoke(
                loadtest,
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
            assert "Route tables for DynamoDB endpoint" in result.output

            call_kwargs = mock_cfn.create_stack.call_args[1]
            params = {p["ParameterKey"]: p["ParameterValue"] for p in call_kwargs["Parameters"]}
            assert params["PrivateRouteTableIds"] == "rtb-123"

    def test_dynamodb_endpoint_uses_resolved_region(self, runner, tmp_path):
        """Deploy uses ec2.meta.region_name for endpoint check, not raw --region."""
        with (
            patch("boto3.client") as mock_client,
            patch("zae_limiter.loadtest.builder.build_and_push_locust_image") as mock_build,
            patch(
                "zae_limiter.loadtest.lambda_builder.build_load_lambda_package"
            ) as mock_lambda_pkg,
            patch("zae_limiter.loadtest.builder.get_zae_limiter_source") as mock_source,
        ):
            mock_cfn, mock_lambda_client, client_factory = self._deploy_base_mocks()
            mock_ec2 = MagicMock()
            mock_ec2.meta.region_name = "eu-west-1"

            def full_client_factory(service, **kwargs):
                if service == "ec2":
                    return mock_ec2
                return client_factory(service, **kwargs)

            mock_client.side_effect = full_client_factory

            mock_ec2.describe_route_tables.return_value = {
                "RouteTables": [{"RouteTableId": "rtb-456"}]
            }
            mock_ec2.describe_vpc_endpoints.return_value = {"VpcEndpoints": []}

            mock_source.return_value = "0.8.0"
            mock_build.return_value = "123.dkr.ecr.eu-west-1.amazonaws.com/test:latest"
            zip_path = tmp_path / "lambda.zip"
            zip_path.write_bytes(b"fake zip")
            mock_lambda_pkg.return_value = zip_path

            result = runner.invoke(
                loadtest,
                [
                    "deploy",
                    "--name",
                    "my-app",
                    "--vpc-id",
                    "vpc-123",
                    "--subnet-ids",
                    "subnet-a",
                    "-C",
                    str(tmp_path),
                ],
            )
            assert result.exit_code == 0, result.output

            # Verify the filter used resolved region, not None
            call_args = mock_ec2.describe_vpc_endpoints.call_args
            filters = call_args[1]["Filters"]
            svc_filter = next(f for f in filters if f["Name"] == "service-name")
            assert svc_filter["Values"] == ["com.amazonaws.eu-west-1.dynamodb"]

    def test_update_reraises_other_errors(self, runner, tmp_path):
        """Deploy re-raises non-'no updates' errors during update."""
        with (
            patch("boto3.client") as mock_client,
            patch("zae_limiter.loadtest.builder.get_zae_limiter_source") as mock_source,
        ):
            mock_cfn, mock_lambda_client, client_factory = self._deploy_base_mocks()
            mock_client.side_effect = client_factory

            mock_cfn.create_stack.side_effect = mock_cfn.exceptions.AlreadyExistsException("exists")
            mock_cfn.update_stack.side_effect = Exception("Access denied")

            mock_source.return_value = "0.8.0"

            result = runner.invoke(
                loadtest,
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


class TestDeleteCommand:
    """Tests for the delete command."""

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
                loadtest,
                ["delete", "--name", "my-app", "--yes"],
            )
            assert result.exit_code == 0
            mock_cfn.delete_stack.assert_called_once_with(StackName="my-app-load")
            assert "Stack deleted" in result.output

    def test_confirms_before_delete(self, runner):
        """Teardown asks for confirmation without --yes."""
        with patch("boto3.client"):
            result = runner.invoke(
                loadtest,
                ["delete", "--name", "my-app"],
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
                loadtest,
                ["delete", "--name", "my-app", "--yes"],
            )
            assert result.exit_code == 0
            assert "Stack deleted" in result.output


class TestListCommand:
    """Tests for the list command."""

    def test_list_no_stacks(self, runner):
        """List shows helpful message when no stacks found."""
        mock_discovery = MagicMock()
        mock_discovery.__aenter__ = AsyncMock(return_value=mock_discovery)
        mock_discovery.__aexit__ = AsyncMock(return_value=False)
        mock_discovery.list_limiters = AsyncMock(return_value=[])

        with patch(
            "zae_limiter.infra.discovery.InfrastructureDiscovery",
            return_value=mock_discovery,
        ):
            result = runner.invoke(loadtest, ["list"])
            assert result.exit_code == 0
            assert "No load test stacks found" in result.output
            assert "zae-limiter loadtest deploy" in result.output

    def test_list_shows_stacks(self, runner):
        """List displays stacks in table format."""
        mock_discovery = MagicMock()
        mock_discovery.__aenter__ = AsyncMock(return_value=mock_discovery)
        mock_discovery.__aexit__ = AsyncMock(return_value=False)

        mock_info = MagicMock()
        mock_info.stack_name = "my-app-load"
        mock_info.user_name = "my-app"
        mock_info.stack_status = "CREATE_COMPLETE"
        mock_info.creation_time = "2026-01-15T12:00:00Z"
        mock_discovery.list_limiters = AsyncMock(return_value=[mock_info])

        with patch(
            "zae_limiter.infra.discovery.InfrastructureDiscovery",
            return_value=mock_discovery,
        ):
            result = runner.invoke(loadtest, ["list"])
            assert result.exit_code == 0
            assert "my-app-load" in result.output
            assert "my-app" in result.output
            assert "CREATE_COMPLETE" in result.output
            assert "Total: 1 stack(s)" in result.output

    def test_list_handles_bad_creation_time(self, runner):
        """List shows 'unknown' for unparseable creation times."""
        mock_discovery = MagicMock()
        mock_discovery.__aenter__ = AsyncMock(return_value=mock_discovery)
        mock_discovery.__aexit__ = AsyncMock(return_value=False)

        mock_info = MagicMock()
        mock_info.stack_name = "my-app-load"
        mock_info.user_name = "my-app"
        mock_info.stack_status = "CREATE_COMPLETE"
        mock_info.creation_time = "not-a-date"
        mock_discovery.list_limiters = AsyncMock(return_value=[mock_info])

        with patch(
            "zae_limiter.infra.discovery.InfrastructureDiscovery",
            return_value=mock_discovery,
        ):
            result = runner.invoke(loadtest, ["list"])
            assert result.exit_code == 0
            assert "unknown" in result.output

    def test_list_with_region(self, runner):
        """List passes region and displays it."""
        mock_discovery = MagicMock()
        mock_discovery.__aenter__ = AsyncMock(return_value=mock_discovery)
        mock_discovery.__aexit__ = AsyncMock(return_value=False)
        mock_discovery.list_limiters = AsyncMock(return_value=[])

        with patch(
            "zae_limiter.infra.discovery.InfrastructureDiscovery",
            return_value=mock_discovery,
        ):
            result = runner.invoke(loadtest, ["list", "--region", "eu-west-1"])
            assert result.exit_code == 0
            assert "eu-west-1" in result.output


class TestUiCommand:
    """Tests for the ui command."""

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
                loadtest,
                ["ui", "--name", "my-app", "-f", "locustfiles/simple.py", "--destroy"],
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
                loadtest,
                ["ui", "--name", "my-app", "-f", "locustfiles/simple.py", "--destroy"],
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
                loadtest,
                ["ui", "--name", "my-app", "-f", "locustfiles/simple.py", "--destroy"],
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
                loadtest,
                ["ui", "--name", "my-app", "-f", "locustfiles/simple.py", "--destroy"],
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
                loadtest,
                ["ui", "--name", "my-app", "-f", "locustfiles/simple.py"],
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
                loadtest,
                ["ui", "--name", "my-app", "-f", "locustfiles/simple.py"],
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
                loadtest,
                ["ui", "--name", "my-app", "-f", "locustfiles/simple.py", "--destroy"],
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
                loadtest,
                ["ui", "--name", "my-app", "-f", "locustfiles/simple.py", "--destroy"],
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
                loadtest,
                ["ui", "--name", "my-app", "-f", "locustfiles/simple.py", "--destroy"],
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
                loadtest,
                ["ui", "--name", "my-app", "-f", "locustfiles/simple.py", "--destroy"],
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
                loadtest,
                ["ui", "--name", "my-app", "-f", "locustfiles/simple.py"],  # no --destroy
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
                loadtest,
                ["ui", "--name", "my-app", "-f", "locustfiles/simple.py", "--destroy"],
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
                loadtest,
                [
                    "ui",
                    "--name",
                    "my-app",
                    "-f",
                    "locustfiles/simple.py",
                    "--max-workers",
                    "50",
                    "--destroy",
                ],
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
                loadtest,
                [
                    "ui",
                    "--name",
                    "my-app",
                    "-f",
                    "locustfiles/simple.py",
                    "--max-workers",
                    "50",
                    "--force",
                    "--destroy",
                ],
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
                loadtest,
                [
                    "ui",
                    "--name",
                    "my-app",
                    "-f",
                    "locustfiles/simple.py",
                    "--max-workers",
                    "50",
                    "--destroy",
                ],
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
                loadtest,
                [
                    "ui",
                    "--name",
                    "my-app",
                    "-f",
                    "locustfiles/simple.py",
                    "--standalone",
                    "--destroy",
                ],
            )
            assert result.exit_code == 0, result.output

            # Verify overrides clear master args for standalone mode
            call_kwargs = mock_ecs.run_task.call_args[1]
            overrides = call_kwargs["overrides"]
            master = next(
                c for c in overrides["containerOverrides"] if c["name"] == "locust-master"
            )
            master_args = next(
                e["value"] for e in master["environment"] if e["name"] == "LOCUST_MASTER_ARGS"
            )
            assert master_args == ""


class TestBuildTaskOverrides:
    """Tests for _build_task_overrides helper."""

    def test_empty_overrides_returns_empty_dict(self):
        from zae_limiter.loadtest.cli import _build_task_overrides

        result = _build_task_overrides(
            standalone=False,
            locustfile=None,
            max_workers=None,
            min_workers=None,
            users_per_worker=None,
            startup_lead_time=None,
        )
        assert result == {}

    def test_scaling_params_override_orchestrator_env(self):
        from zae_limiter.loadtest.cli import _build_task_overrides

        result = _build_task_overrides(
            standalone=False,
            locustfile=None,
            max_workers=50,
            min_workers=2,
            users_per_worker=25,
            startup_lead_time=30,
        )

        orchestrator = next(
            c for c in result["containerOverrides"] if c["name"] == "worker-orchestrator"
        )
        env_dict = {e["name"]: e["value"] for e in orchestrator["environment"]}

        assert env_dict["MAX_WORKERS"] == "50"
        assert env_dict["MIN_WORKERS"] == "2"
        assert env_dict["USERS_PER_WORKER"] == "25"
        assert env_dict["STARTUP_LEAD_TIME"] == "30"

    def test_locustfile_overrides_both_containers(self):
        from zae_limiter.loadtest.cli import _build_task_overrides

        result = _build_task_overrides(
            standalone=False,
            locustfile="locustfiles/llm_production.py",
            max_workers=None,
            min_workers=None,
            users_per_worker=None,
            startup_lead_time=None,
        )

        for container_name in ["locust-master", "worker-orchestrator"]:
            container = next(c for c in result["containerOverrides"] if c["name"] == container_name)
            env_dict = {e["name"]: e["value"] for e in container["environment"]}
            assert env_dict["LOCUSTFILE"] == "locustfiles/llm_production.py"

    def test_standalone_mode_clears_master_args(self):
        from zae_limiter.loadtest.cli import _build_task_overrides

        result = _build_task_overrides(
            standalone=True,
            locustfile="simple.py",
            max_workers=None,
            min_workers=None,
            users_per_worker=None,
            startup_lead_time=None,
        )

        master = next(c for c in result["containerOverrides"] if c["name"] == "locust-master")
        env_dict = {e["name"]: e["value"] for e in master["environment"]}
        assert env_dict["LOCUST_MASTER_ARGS"] == ""
        assert env_dict["LOCUSTFILE"] == "simple.py"

    def test_standalone_mode_sets_orchestrator_idle(self):
        from zae_limiter.loadtest.cli import _build_task_overrides

        result = _build_task_overrides(
            standalone=True,
            locustfile=None,
            max_workers=None,
            min_workers=None,
            users_per_worker=None,
            startup_lead_time=None,
        )

        orchestrator = next(
            c for c in result["containerOverrides"] if c["name"] == "worker-orchestrator"
        )
        env_dict = {e["name"]: e["value"] for e in orchestrator["environment"]}
        assert env_dict["MAX_WORKERS"] == "0"
        assert env_dict["MIN_WORKERS"] == "0"


class TestGetServiceNetworkConfig:
    """Tests for _get_service_network_config helper."""

    def test_extracts_network_config_from_service(self):
        from zae_limiter.loadtest.cli import _get_service_network_config

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

    def test_raises_error_when_service_not_found(self):
        from zae_limiter.loadtest.cli import _get_service_network_config

        mock_ecs = MagicMock()
        mock_ecs.describe_services.return_value = {"services": []}

        with pytest.raises(click.ClickException, match="Service not found"):
            _get_service_network_config(mock_ecs, "my-cluster", "nonexistent-service")


# ---------------------------------------------------------------------------
# Invoke Lambda Headless helper
# ---------------------------------------------------------------------------


class TestInvokeLambdaHeadless:
    """Tests for _invoke_lambda_headless helper."""

    def test_returns_stats_on_success(self):
        import json

        from zae_limiter.loadtest.cli import _invoke_lambda_headless

        mock_lambda = MagicMock()
        payload_data = {
            "total_requests": 500,
            "requests_per_second": 50.0,
            "p50": 20,
            "p95": 35,
            "p99": 50,
        }
        mock_lambda.invoke.return_value = {
            "Payload": MagicMock(read=MagicMock(return_value=json.dumps(payload_data).encode()))
        }

        result = _invoke_lambda_headless(
            lambda_client=mock_lambda,
            func_name="test-func",
            users=10,
            duration=30,
            spawn_rate=10,
            locustfile="locustfiles/max_rps.py",
        )
        assert result["total_requests"] == 500
        assert result["p50"] == 20

    def test_passes_user_classes(self):
        import json

        from zae_limiter.loadtest.cli import _invoke_lambda_headless

        mock_lambda = MagicMock()
        mock_lambda.invoke.return_value = {
            "Payload": MagicMock(read=MagicMock(return_value=json.dumps({"p50": 10}).encode()))
        }

        _invoke_lambda_headless(
            lambda_client=mock_lambda,
            func_name="test-func",
            users=5,
            duration=15,
            spawn_rate=5,
            locustfile="locustfiles/max_rps.py",
            user_classes="MaxRpsCascadeUser",
        )

        call_payload = json.loads(mock_lambda.invoke.call_args[1]["Payload"])
        assert call_payload["config"]["user_classes"] == "MaxRpsCascadeUser"

    def test_raises_on_lambda_error(self):
        import json

        from zae_limiter.loadtest.cli import _invoke_lambda_headless

        mock_lambda = MagicMock()
        error_payload = {
            "errorMessage": "Task timed out",
            "stackTrace": ["line 1", "line 2"],
        }
        mock_lambda.invoke.return_value = {
            "Payload": MagicMock(read=MagicMock(return_value=json.dumps(error_payload).encode()))
        }

        with pytest.raises(click.ClickException, match="Task timed out"):
            _invoke_lambda_headless(
                lambda_client=mock_lambda,
                func_name="test-func",
                users=10,
                duration=30,
                spawn_rate=10,
                locustfile="locustfiles/max_rps.py",
            )

    def test_raises_on_error_without_trace(self):
        import json

        from zae_limiter.loadtest.cli import _invoke_lambda_headless

        mock_lambda = MagicMock()
        error_payload = {"errorMessage": "Out of memory"}
        mock_lambda.invoke.return_value = {
            "Payload": MagicMock(read=MagicMock(return_value=json.dumps(error_payload).encode()))
        }

        with pytest.raises(click.ClickException, match="Out of memory"):
            _invoke_lambda_headless(
                lambda_client=mock_lambda,
                func_name="test-func",
                users=10,
                duration=30,
                spawn_rate=10,
                locustfile="locustfiles/max_rps.py",
            )


# ---------------------------------------------------------------------------
# Run command (renamed from benchmark)
# ---------------------------------------------------------------------------


class TestRunCommand:
    """Tests for the run command (renamed from benchmark)."""

    def test_run_command_exists(self, runner):
        """The 'run' subcommand is registered on the load group."""
        result = runner.invoke(loadtest, ["run", "--help"])
        assert result.exit_code == 0
        assert "Run a single load test execution" in result.output

    def test_run_lambda_mode(self, runner):
        """Run in lambda mode invokes Lambda and displays results."""
        with (
            patch("zae_limiter.loadtest.cli._get_lambda_client_and_config") as mock_get_config,
            patch("zae_limiter.loadtest.cli._invoke_lambda_headless") as mock_invoke,
        ):
            mock_lambda = MagicMock()
            mock_get_config.return_value = (mock_lambda, "test-load-worker", 1769, 300)
            mock_invoke.return_value = {
                "total_requests": 1000,
                "total_failures": 0,
                "failure_rate": 0,
                "requests_per_second": 50.0,
                "avg_response_time": 20,
                "min_response_time": 10,
                "max_response_time": 100,
                "p50": 18,
                "p95": 35,
                "p99": 50,
            }

            result = runner.invoke(
                loadtest,
                [
                    "run",
                    "--name",
                    "test",
                    "-f",
                    "locustfiles/max_rps.py",
                    "--users",
                    "10",
                    "--duration",
                    "30",
                ],
            )
            assert result.exit_code == 0, result.output
            assert "Lambda Run" in result.output
            assert "p50" in result.output

    def test_benchmark_command_no_longer_exists(self, runner):
        """The old 'benchmark' subcommand is removed."""
        result = runner.invoke(loadtest, ["benchmark", "--help"])
        assert result.exit_code != 0

    def test_run_workers_triggers_distributed(self, runner):
        """Run with --workers dispatches to distributed mode."""
        with patch("zae_limiter.loadtest.cli._benchmark_distributed") as mock_dist:
            result = runner.invoke(
                loadtest,
                [
                    "run",
                    "--name",
                    "test",
                    "-f",
                    "locustfiles/max_rps.py",
                    "--workers",
                    "4",
                ],
            )
            assert result.exit_code == 0, result.output
            mock_dist.assert_called_once()
            call_kwargs = mock_dist.call_args[1]
            assert call_kwargs["workers"] == 4

    def test_run_standalone_triggers_fargate(self, runner):
        """Run with --standalone dispatches to fargate mode."""
        with patch("zae_limiter.loadtest.cli._benchmark_fargate") as mock_fg:
            result = runner.invoke(
                loadtest,
                [
                    "run",
                    "--name",
                    "test",
                    "-f",
                    "locustfiles/max_rps.py",
                    "--standalone",
                ],
            )
            assert result.exit_code == 0, result.output
            mock_fg.assert_called_once()

    def test_run_help_does_not_show_standalone(self, runner):
        """The --standalone flag is hidden from --help."""
        result = runner.invoke(loadtest, ["run", "--help"])
        assert result.exit_code == 0
        assert "--standalone" not in result.output

    def test_run_help_does_not_show_mode(self, runner):
        """The old --mode option is removed."""
        result = runner.invoke(loadtest, ["run", "--help"])
        assert result.exit_code == 0
        assert "--mode" not in result.output


# ---------------------------------------------------------------------------
# Push command
# ---------------------------------------------------------------------------


class TestPushCommand:
    """Tests for the push command."""

    def test_push_help(self, runner):
        """Push command shows help text."""
        result = runner.invoke(loadtest, ["push", "--help"])
        assert result.exit_code == 0
        assert "Rebuild and push" in result.output

    def test_push_calls_push_code(self, runner, tmp_path):
        """Push rebuilds and pushes code."""
        with (
            patch("zae_limiter.loadtest.cli._push_code") as mock_push_code,
            patch("zae_limiter.loadtest.builder.get_zae_limiter_source", return_value="0.8.0"),
        ):
            result = runner.invoke(
                loadtest,
                [
                    "push",
                    "--name",
                    "my-app",
                    "-C",
                    str(tmp_path),
                ],
            )
            assert result.exit_code == 0, result.output
            mock_push_code.assert_called_once()
            call_args = mock_push_code.call_args
            assert call_args[0][0] == "my-app-load"  # stack_name

    def test_push_interactive_name(self, runner, tmp_path):
        """Push prompts for name when not provided."""
        with (
            patch("zae_limiter.loadtest.cli._push_code"),
            patch("zae_limiter.loadtest.builder.get_zae_limiter_source", return_value="0.8.0"),
            patch("zae_limiter.loadtest.cli._select_name", return_value="my-app") as mock_select,
        ):
            result = runner.invoke(
                loadtest,
                [
                    "push",
                    "-C",
                    str(tmp_path),
                ],
            )
            assert result.exit_code == 0, result.output
            mock_select.assert_called_once()


# ---------------------------------------------------------------------------
# Tune command
# ---------------------------------------------------------------------------


class TestTuneCommand:
    """Tests for the tune command."""

    def test_tune_help(self, runner):
        """Tune command shows help text."""
        result = runner.invoke(loadtest, ["tune", "--help"])
        assert result.exit_code == 0
        assert "binary search" in result.output.lower()

    def test_tune_basic(self, runner):
        """Tune runs binary search and displays results."""
        invoke_count = 0

        def mock_invoke(
            lambda_client, func_name, users, duration, spawn_rate, locustfile, user_classes=None
        ):
            nonlocal invoke_count
            invoke_count += 1
            # Simulate: 1 user = 20ms p50, scaling linearly with users
            p50 = 20 * users
            rps = 50 * users / (users**0.5)  # sub-linear scaling
            reqs = max(int(rps * duration), 200)  # ensure >= min_requests
            return {
                "total_requests": reqs,
                "requests_per_second": rps,
                "p50": p50,
                "p95": p50 * 1.5,
                "p99": p50 * 2,
            }

        with (
            patch("zae_limiter.loadtest.cli._get_lambda_client_and_config") as mock_get_config,
            patch(
                "zae_limiter.loadtest.cli._invoke_lambda_headless",
                side_effect=mock_invoke,
            ),
        ):
            mock_lambda = MagicMock()
            mock_get_config.return_value = (mock_lambda, "test-load-worker", 1769, 300)

            result = runner.invoke(
                loadtest,
                [
                    "tune",
                    "--name",
                    "test",
                    "-f",
                    "locustfiles/max_rps.py",
                    "--max-users",
                    "16",
                    "--threshold",
                    "0.50",
                    "--step-duration",
                    "10",
                    "--baseline-duration",
                    "5",
                ],
            )
            assert result.exit_code == 0, result.output
            assert "Calibration Results" in result.output
            assert "Optimal" in result.output
            assert "Distributed recommendations" in result.output
            # Should have invoked Lambda multiple times
            assert invoke_count >= 3  # baseline + upper + at least 1 search

    def test_tune_efficiency_always_above_threshold(self, runner):
        """When efficiency >= threshold at max_users, reports max_users as optimal."""

        def mock_invoke(
            lambda_client, func_name, users, duration, spawn_rate, locustfile, user_classes=None
        ):
            # Very low latency growth — efficiency always high
            p50 = 20 + users * 0.1
            return {"total_requests": 500, "requests_per_second": 50 * users, "p50": p50}

        with (
            patch("zae_limiter.loadtest.cli._get_lambda_client_and_config") as mock_get_config,
            patch(
                "zae_limiter.loadtest.cli._invoke_lambda_headless",
                side_effect=mock_invoke,
            ),
        ):
            mock_lambda = MagicMock()
            mock_get_config.return_value = (mock_lambda, "test-load-worker", 1769, 300)

            result = runner.invoke(
                loadtest,
                [
                    "tune",
                    "--name",
                    "test",
                    "-f",
                    "locustfiles/max_rps.py",
                    "--max-users",
                    "20",
                    "--threshold",
                    "0.50",
                ],
            )
            assert result.exit_code == 0, result.output
            assert "Optimal: 20 users per worker" in result.output

    def test_tune_zero_baseline_p50_exits(self, runner):
        """Tune exits with error if baseline p50 is zero."""

        def mock_invoke(
            lambda_client, func_name, users, duration, spawn_rate, locustfile, user_classes=None
        ):
            # Return enough requests to pass the min threshold but p50=0
            return {"requests_per_second": 0, "p50": 0, "total_requests": 200}

        with (
            patch("zae_limiter.loadtest.cli._get_lambda_client_and_config") as mock_get_config,
            patch(
                "zae_limiter.loadtest.cli._invoke_lambda_headless",
                side_effect=mock_invoke,
            ),
        ):
            mock_lambda = MagicMock()
            mock_get_config.return_value = (mock_lambda, "test-load-worker", 1769, 300)

            result = runner.invoke(
                loadtest,
                [
                    "tune",
                    "--name",
                    "test",
                    "-f",
                    "locustfiles/max_rps.py",
                ],
            )
            assert result.exit_code != 0

    def test_tune_with_user_classes(self, runner):
        """Tune passes --user-classes to Lambda invocations."""
        captured_user_classes = []

        def mock_invoke(
            lambda_client, func_name, users, duration, spawn_rate, locustfile, user_classes=None
        ):
            captured_user_classes.append(user_classes)
            p50 = 20 + users * 5
            return {"requests_per_second": 50, "p50": p50, "total_requests": 500}

        with (
            patch("zae_limiter.loadtest.cli._get_lambda_client_and_config") as mock_get_config,
            patch(
                "zae_limiter.loadtest.cli._invoke_lambda_headless",
                side_effect=mock_invoke,
            ),
        ):
            mock_lambda = MagicMock()
            mock_get_config.return_value = (mock_lambda, "test-load-worker", 1769, 300)

            result = runner.invoke(
                loadtest,
                [
                    "tune",
                    "--name",
                    "test",
                    "-f",
                    "locustfiles/max_rps.py",
                    "--user-classes",
                    "MaxRpsCascadeUser",
                    "--max-users",
                    "4",
                ],
            )
            assert result.exit_code == 0, result.output
            assert all(uc == "MaxRpsCascadeUser" for uc in captured_user_classes)


# ---------------------------------------------------------------------------
# Display calibration results
# ---------------------------------------------------------------------------


class TestDisplayCalibrationResults:
    """Tests for _display_calibration_results helper."""

    def test_displays_table_and_recommendations(self, capsys):
        from zae_limiter.loadtest.cli import _display_calibration_results

        steps = [
            {
                "users": 1,
                "rps": 47.0,
                "p50": 20.0,
                "p95": 30.0,
                "p99": 40.0,
                "efficiency": 1.0,
                "requests": 705,
            },
            {
                "users": 40,
                "rps": 381.0,
                "p50": 95.0,
                "p95": 140.0,
                "p99": 190.0,
                "efficiency": 0.21,
                "requests": 11430,
            },
            {
                "users": 20,
                "rps": 383.0,
                "p50": 47.0,
                "p95": 70.0,
                "p99": 94.0,
                "efficiency": 0.43,
                "requests": 11490,
            },
            {
                "users": 10,
                "rps": 357.0,
                "p50": 26.0,
                "p95": 39.0,
                "p99": 52.0,
                "efficiency": 0.77,
                "requests": 10710,
            },
            {
                "users": 5,
                "rps": 200.0,
                "p50": 22.0,
                "p95": 33.0,
                "p99": 44.0,
                "efficiency": 0.91,
                "requests": 6000,
            },
            {
                "users": 7,
                "rps": 280.0,
                "p50": 24.0,
                "p95": 36.0,
                "p99": 48.0,
                "efficiency": 0.83,
                "requests": 8400,
            },
        ]

        _display_calibration_results(
            steps=steps,
            baseline_p50=20.0,
            optimal_users=7,
            optimal_rps=280.0,
            threshold=0.80,
        )
        captured = capsys.readouterr()

        assert "Calibration Results (threshold: 80%)" in captured.out
        assert "RPS" in captured.out
        assert "p95" in captured.out
        assert "p99" in captured.out
        assert "Reqs" in captured.out
        assert "(baseline)" in captured.out
        assert "<- optimal (>= 80%)" in captured.out
        assert "Optimal: 7 users per worker" in captured.out
        assert "Floor latency: p50=20ms, p95=30ms, p99=40ms" in captured.out
        assert "Throughput per worker: 280.0 RPS" in captured.out
        assert "Distributed recommendations" in captured.out
        assert "loadtest run" in captured.out

    def test_recommendations_compute_workers_correctly(self, capsys):
        from zae_limiter.loadtest.cli import _display_calibration_results

        steps = [
            {
                "users": 1,
                "rps": 50.0,
                "p50": 20.0,
                "p95": 30.0,
                "p99": 40.0,
                "efficiency": 1.0,
                "requests": 500,
            },
            {
                "users": 10,
                "rps": 350.0,
                "p50": 25.0,
                "p95": 37.0,
                "p99": 50.0,
                "efficiency": 0.80,
                "requests": 10500,
            },
        ]

        _display_calibration_results(
            steps=steps,
            baseline_p50=20.0,
            optimal_users=10,
            optimal_rps=350.0,
            threshold=0.80,
        )
        captured = capsys.readouterr()

        # 100 / 10 = 10 workers
        assert "--workers 10 --users 100" in captured.out
        # 500 / 10 = 50 workers
        assert "--workers 50 --users 500" in captured.out
        # 1000 / 10 = 100 workers
        assert "--workers 100 --users 1000" in captured.out
