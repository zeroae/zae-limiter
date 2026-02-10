"""Tests for load test CLI commands."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

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
