"""Tests for the limits CLI commands."""

import io
import json
import tempfile
from unittest.mock import AsyncMock, MagicMock, patch

import yaml
from click.testing import CliRunner

from zae_limiter.cli import cli


class TestLimitsPlan:
    """Tests for `zae-limiter limits plan -f <file>`."""

    def test_plan_shows_changes(self):
        """Plan command parses YAML, invokes Lambda, and shows diff."""
        yaml_content = {
            "namespace": "test-ns",
            "system": {"limits": {"rpm": {"capacity": 1000}}},
        }
        with tempfile.NamedTemporaryFile(suffix=".yaml", mode="w", delete=False) as f:
            yaml.dump(yaml_content, f)
            f.flush()

            runner = CliRunner()
            with patch("zae_limiter.limits_cli._invoke_provisioner") as mock_invoke:
                mock_invoke.return_value = {
                    "status": "planned",
                    "changes": [
                        {"action": "create", "level": "system", "target": None},
                    ],
                }
                result = runner.invoke(
                    cli,
                    [
                        "limits",
                        "plan",
                        "--name",
                        "test-app",
                        "--region",
                        "us-east-1",
                        "-f",
                        f.name,
                    ],
                )
                assert result.exit_code == 0
                assert "create" in result.output
                assert "system" in result.output

    def test_plan_no_changes(self):
        """Plan with no changes shows up-to-date message."""
        yaml_content = {"namespace": "test-ns"}
        with tempfile.NamedTemporaryFile(suffix=".yaml", mode="w", delete=False) as f:
            yaml.dump(yaml_content, f)
            f.flush()

            runner = CliRunner()
            with patch("zae_limiter.limits_cli._invoke_provisioner") as mock_invoke:
                mock_invoke.return_value = {"status": "planned", "changes": []}
                result = runner.invoke(
                    cli,
                    ["limits", "plan", "--name", "test-app", "-f", f.name],
                )
                assert result.exit_code == 0
                assert "up-to-date" in result.output.lower()


class TestLimitsApply:
    """Tests for `zae-limiter limits apply -f <file>`."""

    def test_apply_invokes_lambda(self):
        """Apply command parses YAML and invokes Lambda with action=apply."""
        yaml_content = {
            "namespace": "test-ns",
            "resources": {"gpt-4": {"limits": {"rpm": {"capacity": 1000}}}},
        }
        with tempfile.NamedTemporaryFile(suffix=".yaml", mode="w", delete=False) as f:
            yaml.dump(yaml_content, f)
            f.flush()

            runner = CliRunner()
            with patch("zae_limiter.limits_cli._invoke_provisioner") as mock_invoke:
                mock_invoke.return_value = {
                    "status": "applied",
                    "changes": [
                        {
                            "action": "create",
                            "level": "resource",
                            "target": "gpt-4",
                        },
                    ],
                    "created": 1,
                    "updated": 0,
                    "deleted": 0,
                    "errors": [],
                }
                result = runner.invoke(
                    cli,
                    [
                        "limits",
                        "apply",
                        "--name",
                        "test-app",
                        "--region",
                        "us-east-1",
                        "-f",
                        f.name,
                    ],
                )
                assert result.exit_code == 0
                assert "create" in result.output.lower()


class TestLimitsApplyNoChanges:
    """Tests for apply with no changes."""

    def test_apply_no_changes(self):
        """Apply with no changes shows up-to-date message."""
        yaml_content = {"namespace": "test-ns"}
        with tempfile.NamedTemporaryFile(suffix=".yaml", mode="w", delete=False) as f:
            yaml.dump(yaml_content, f)
            f.flush()

            runner = CliRunner()
            with patch("zae_limiter.limits_cli._invoke_provisioner") as mock_invoke:
                mock_invoke.return_value = {"status": "applied", "changes": []}
                result = runner.invoke(
                    cli,
                    ["limits", "apply", "--name", "test-app", "-f", f.name],
                )
                assert result.exit_code == 0
                assert "up-to-date" in result.output.lower()

    def test_apply_with_errors_exits_nonzero(self):
        """Apply with errors exits with code 1."""
        yaml_content = {
            "namespace": "test-ns",
            "resources": {"gpt-4": {"limits": {"rpm": {"capacity": 1000}}}},
        }
        with tempfile.NamedTemporaryFile(suffix=".yaml", mode="w", delete=False) as f:
            yaml.dump(yaml_content, f)
            f.flush()

            runner = CliRunner()
            with patch("zae_limiter.limits_cli._invoke_provisioner") as mock_invoke:
                mock_invoke.return_value = {
                    "status": "applied",
                    "changes": [{"action": "create", "level": "resource", "target": "gpt-4"}],
                    "created": 0,
                    "updated": 0,
                    "deleted": 0,
                    "errors": ["ConditionalCheckFailed for gpt-4"],
                }
                result = runner.invoke(
                    cli,
                    ["limits", "apply", "--name", "test-app", "-f", f.name],
                )
                assert result.exit_code == 1


class TestLimitsDiff:
    """Tests for `zae-limiter limits diff -f <file>`."""

    def test_diff_shows_drift(self):
        """Diff command shows detected drift."""
        yaml_content = {
            "namespace": "test-ns",
            "resources": {"gpt-4": {"limits": {"rpm": {"capacity": 1000}}}},
        }
        with tempfile.NamedTemporaryFile(suffix=".yaml", mode="w", delete=False) as f:
            yaml.dump(yaml_content, f)
            f.flush()

            runner = CliRunner()
            with patch("zae_limiter.limits_cli._invoke_provisioner") as mock_invoke:
                mock_invoke.return_value = {
                    "status": "planned",
                    "changes": [
                        {"action": "create", "level": "resource", "target": "gpt-4"},
                    ],
                }
                result = runner.invoke(
                    cli,
                    ["limits", "diff", "--name", "test-app", "-f", f.name],
                )
                assert result.exit_code == 0
                assert "drift detected" in result.output.lower()
                assert "resource" in result.output

    def test_diff_no_drift(self):
        """Diff with no drift shows matching message."""
        yaml_content = {"namespace": "test-ns"}
        with tempfile.NamedTemporaryFile(suffix=".yaml", mode="w", delete=False) as f:
            yaml.dump(yaml_content, f)
            f.flush()

            runner = CliRunner()
            with patch("zae_limiter.limits_cli._invoke_provisioner") as mock_invoke:
                mock_invoke.return_value = {"status": "planned", "changes": []}
                result = runner.invoke(
                    cli,
                    ["limits", "diff", "--name", "test-app", "-f", f.name],
                )
                assert result.exit_code == 0
                assert "no drift" in result.output.lower()


class TestLimitsCfnTemplate:
    """Tests for `zae-limiter limits cfn-template -f <file>`."""

    def test_cfn_template_output(self):
        """cfn-template command generates valid CFN template."""
        yaml_content = {
            "namespace": "test-ns",
            "resources": {"gpt-4": {"limits": {"rpm": {"capacity": 1000}}}},
        }
        with tempfile.NamedTemporaryFile(suffix=".yaml", mode="w", delete=False) as f:
            yaml.dump(yaml_content, f)
            f.flush()

            runner = CliRunner()
            result = runner.invoke(
                cli,
                [
                    "limits",
                    "cfn-template",
                    "--name",
                    "test-app",
                    "-f",
                    f.name,
                ],
            )
            assert result.exit_code == 0
            assert "Custom::ZaeLimiterLimits" in result.output
            assert "ServiceToken" in result.output

    def test_cfn_template_with_system_and_entities(self):
        """cfn-template includes system, resources, and entities sections."""
        yaml_content = {
            "namespace": "test-ns",
            "system": {
                "on_unavailable": "allow",
                "limits": {"rpm": {"capacity": 1000}},
            },
            "resources": {"gpt-4": {"limits": {"rpm": {"capacity": 500}}}},
            "entities": {
                "user-123": {
                    "resources": {
                        "gpt-4": {"limits": {"rpm": {"capacity": 100}}},
                    },
                },
            },
        }
        with tempfile.NamedTemporaryFile(suffix=".yaml", mode="w", delete=False) as f:
            yaml.dump(yaml_content, f)
            f.flush()

            runner = CliRunner()
            result = runner.invoke(
                cli,
                ["limits", "cfn-template", "--name", "test-app", "-f", f.name],
            )
            assert result.exit_code == 0
            parsed = yaml.safe_load(result.output)
            props = parsed["Resources"]["TenantLimits"]["Properties"]
            assert "System" in props
            assert props["System"]["OnUnavailable"] == "allow"
            assert "Entities" in props
            assert "user-123" in props["Entities"]

    def test_cfn_template_limits_with_burst_and_refill(self):
        """cfn-template converts burst/refill fields to PascalCase."""
        yaml_content = {
            "namespace": "test-ns",
            "resources": {
                "gpt-4": {
                    "limits": {
                        "rpm": {
                            "capacity": 1000,
                            "burst": 1500,
                            "refill_amount": 100,
                            "refill_period": 60,
                        },
                    },
                },
            },
        }
        with tempfile.NamedTemporaryFile(suffix=".yaml", mode="w", delete=False) as f:
            yaml.dump(yaml_content, f)
            f.flush()

            runner = CliRunner()
            result = runner.invoke(
                cli,
                ["limits", "cfn-template", "--name", "test-app", "-f", f.name],
            )
            assert result.exit_code == 0
            parsed = yaml.safe_load(result.output)
            rpm = parsed["Resources"]["TenantLimits"]["Properties"]["Resources"]["gpt-4"]["Limits"][
                "rpm"
            ]
            assert rpm["Capacity"] == 1000
            assert rpm["Burst"] == 1500
            assert rpm["RefillAmount"] == 100
            assert rpm["RefillPeriod"] == 60


class TestLoadYaml:
    """Tests for _load_yaml helper."""

    def test_load_yaml_rejects_non_dict(self):
        """_load_yaml exits with error for non-dict YAML."""
        with tempfile.NamedTemporaryFile(suffix=".yaml", mode="w", delete=False) as f:
            f.write("- item1\n- item2\n")
            f.flush()

            runner = CliRunner()
            with patch("zae_limiter.limits_cli._invoke_provisioner"):
                result = runner.invoke(
                    cli,
                    ["limits", "plan", "--name", "test-app", "-f", f.name],
                )
                assert result.exit_code != 0


class TestInvokeProvisioner:
    """Tests for _invoke_provisioner helper."""

    def test_invoke_provisioner_calls_lambda(self):
        """_invoke_provisioner invokes Lambda with correct payload."""
        yaml_content = {"namespace": "test-ns"}
        with tempfile.NamedTemporaryFile(suffix=".yaml", mode="w", delete=False) as f:
            yaml.dump(yaml_content, f)
            f.flush()

            runner = CliRunner()
            with (
                patch("asyncio.run", side_effect=Exception("no repo")),
                patch("zae_limiter.limits_cli.boto3.client") as mock_boto3_client,
            ):
                mock_lambda = MagicMock()
                response_payload = {"status": "planned", "changes": []}
                mock_lambda.invoke.return_value = {
                    "Payload": io.BytesIO(json.dumps(response_payload).encode()),
                }
                mock_boto3_client.return_value = mock_lambda

                result = runner.invoke(
                    cli,
                    [
                        "limits",
                        "plan",
                        "--name",
                        "test-app",
                        "--region",
                        "us-east-1",
                        "--endpoint-url",
                        "http://localhost:4566",
                        "-f",
                        f.name,
                    ],
                )
                assert result.exit_code == 0
                mock_boto3_client.assert_called_once_with(
                    "lambda",
                    region_name="us-east-1",
                    endpoint_url="http://localhost:4566",
                )
                call_args = mock_lambda.invoke.call_args
                payload = json.loads(call_args[1]["Payload"])
                assert payload["action"] == "plan"
                assert payload["table_name"] == "test-app"

    def test_invoke_provisioner_resolves_namespace(self):
        """_invoke_provisioner resolves namespace_id via Repository.connect()."""
        yaml_content = {"namespace": "test-ns"}
        with tempfile.NamedTemporaryFile(suffix=".yaml", mode="w", delete=False) as f:
            yaml.dump(yaml_content, f)
            f.flush()

            runner = CliRunner()
            mock_repo = MagicMock()
            mock_repo._namespace_id = "abc123"
            mock_repo.close = AsyncMock()

            async def _mock_connect(*args, **kwargs):
                return mock_repo

            with (
                patch(
                    "zae_limiter.repository.Repository.connect",
                    side_effect=_mock_connect,
                ),
                patch("zae_limiter.limits_cli.boto3.client") as mock_boto3_client,
            ):
                mock_lambda = MagicMock()
                response_payload = {"status": "planned", "changes": []}
                mock_lambda.invoke.return_value = {
                    "Payload": io.BytesIO(json.dumps(response_payload).encode()),
                }
                mock_boto3_client.return_value = mock_lambda

                result = runner.invoke(
                    cli,
                    ["limits", "plan", "--name", "test-app", "-f", f.name],
                )
                assert result.exit_code == 0
                call_args = mock_lambda.invoke.call_args
                payload = json.loads(call_args[1]["Payload"])
                assert payload["namespace_id"] == "abc123"

    def test_invoke_provisioner_auto_registers_namespace(self):
        """_invoke_provisioner auto-registers namespace on NamespaceNotFoundError."""
        from zae_limiter.exceptions import NamespaceNotFoundError

        yaml_content = {"namespace": "new-ns"}
        with tempfile.NamedTemporaryFile(suffix=".yaml", mode="w", delete=False) as f:
            yaml.dump(yaml_content, f)
            f.flush()

            runner = CliRunner()

            # First connect (with namespace="new-ns") raises NamespaceNotFoundError
            # Second connect (default namespace) succeeds
            mock_default_repo = MagicMock()
            mock_default_repo.close = AsyncMock()
            mock_default_repo.register_namespace = AsyncMock()

            mock_scoped_repo = MagicMock()
            mock_scoped_repo._namespace_id = "new-ns-id"

            mock_default_repo.namespace = AsyncMock(return_value=mock_scoped_repo)

            call_count = 0

            async def _mock_connect(*args, **kwargs):
                nonlocal call_count
                call_count += 1
                if call_count == 1:
                    # First call with namespace="new-ns" fails
                    raise NamespaceNotFoundError("new-ns")
                # Second call (default namespace) succeeds
                return mock_default_repo

            with (
                patch(
                    "zae_limiter.repository.Repository.connect",
                    side_effect=_mock_connect,
                ),
                patch("zae_limiter.limits_cli.boto3.client") as mock_boto3_client,
            ):
                mock_lambda = MagicMock()
                response_payload = {"status": "planned", "changes": []}
                mock_lambda.invoke.return_value = {
                    "Payload": io.BytesIO(json.dumps(response_payload).encode()),
                }
                mock_boto3_client.return_value = mock_lambda

                result = runner.invoke(
                    cli,
                    ["limits", "plan", "--name", "test-app", "-f", f.name],
                )
                assert result.exit_code == 0
                # Verify namespace was auto-registered
                mock_default_repo.register_namespace.assert_awaited_once_with("new-ns")
                mock_default_repo.namespace.assert_awaited_once_with("new-ns")
                # Verify the resolved namespace_id was used
                call_args = mock_lambda.invoke.call_args
                payload = json.loads(call_args[1]["Payload"])
                assert payload["namespace_id"] == "new-ns-id"

    def test_invoke_provisioner_lambda_error_exits(self):
        """_invoke_provisioner exits on Lambda error response."""
        yaml_content = {"namespace": "test-ns"}
        with tempfile.NamedTemporaryFile(suffix=".yaml", mode="w", delete=False) as f:
            yaml.dump(yaml_content, f)
            f.flush()

            runner = CliRunner()
            with (
                patch("asyncio.run", side_effect=Exception("no repo")),
                patch("zae_limiter.limits_cli.boto3.client") as mock_boto3_client,
            ):
                mock_lambda = MagicMock()
                response_payload = {"errorMessage": "Something went wrong"}
                mock_lambda.invoke.return_value = {
                    "Payload": io.BytesIO(json.dumps(response_payload).encode()),
                }
                mock_boto3_client.return_value = mock_lambda

                result = runner.invoke(
                    cli,
                    ["limits", "plan", "--name", "test-app", "-f", f.name],
                )
                assert result.exit_code != 0
