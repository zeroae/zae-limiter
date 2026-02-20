"""Tests for the limits CLI commands."""

import tempfile
from unittest.mock import patch

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
