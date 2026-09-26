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

    def test_cfn_template_limits_with_refill(self):
        """cfn-template converts refill fields to PascalCase."""
        yaml_content = {
            "namespace": "test-ns",
            "resources": {
                "gpt-4": {
                    "limits": {
                        "rpm": {
                            "capacity": 1000,
                            "refill_amount": 100,
                            "refill_period": 60,
                        },
                    },
                },
            },
        }
        with tempfile.NamedTemporaryFile(suffix=".yaml", mode="w", delete=False) as f:
            yaml.dump(yaml_content, f)

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
            assert "Burst" not in rpm
            assert rpm["RefillAmount"] == 100
            assert rpm["RefillPeriod"] == 60


class TestLimitsCfnTemplateDisabled:
    """Tests for the `disabled` manifest key -> CFN `Disabled` property tri-state emission.

    The generator (`limits cfn-template`) and the provisioner's consumer
    (`_cfn_properties_to_manifest`) must agree on the wire format, or a YAML
    manifest with `disabled: true` would silently produce a template that never
    disables anything.
    """

    def _run_cfn_template(self, yaml_content: dict) -> dict:
        with tempfile.NamedTemporaryFile(suffix=".yaml", mode="w", delete=False) as f:
            yaml.dump(yaml_content, f)
            f.flush()

            runner = CliRunner()
            result = runner.invoke(
                cli,
                ["limits", "cfn-template", "--name", "test-app", "-f", f.name],
            )
            assert result.exit_code == 0
            parsed: dict = yaml.safe_load(result.output)
            return parsed["Resources"]["TenantLimits"]["Properties"]

    def test_resource_disabled_true_emitted(self):
        props = self._run_cfn_template(
            {
                "namespace": "test-ns",
                "resources": {"gpt-4": {"disabled": True, "limits": {"rpm": {"capacity": 1000}}}},
            }
        )
        assert props["Resources"]["gpt-4"]["Disabled"] is True

    def test_resource_disabled_omitted_when_absent(self):
        """No `disabled` key in the manifest means "inherit" — must stay absent from the CFN
        template."""
        props = self._run_cfn_template(
            {
                "namespace": "test-ns",
                "resources": {"gpt-4": {"limits": {"rpm": {"capacity": 1000}}}},
            }
        )
        assert "Disabled" not in props["Resources"]["gpt-4"]

    def test_entity_resource_disabled_false_emitted(self):
        """False is the carve-out value — it must round-trip, not be coerced or dropped."""
        props = self._run_cfn_template(
            {
                "namespace": "test-ns",
                "entities": {
                    "vip-1": {
                        "resources": {
                            "gpt-4": {"disabled": False, "limits": {"rpm": {"capacity": 1000}}}
                        }
                    }
                },
            }
        )
        assert props["Entities"]["vip-1"]["Resources"]["gpt-4"]["Disabled"] is False

    def test_entity_resource_disabled_omitted_when_absent(self):
        props = self._run_cfn_template(
            {
                "namespace": "test-ns",
                "entities": {
                    "vip-1": {"resources": {"gpt-4": {"limits": {"rpm": {"capacity": 1000}}}}}
                },
            }
        )
        assert "Disabled" not in props["Entities"]["vip-1"]["Resources"]["gpt-4"]

    def test_round_trip_resource_and_entity_disabled_survive_cfn_conversion(self):
        """Manifest -> CFN template -> `_cfn_properties_to_manifest` reproduces the tri-state.

        Exercises the generator (limits_cli.py) and the provisioner's consumer
        (zae_limiter_provisioner.handler) together to prove the wire format they
        share actually agrees in both directions.
        """
        from zae_limiter_provisioner.handler import _cfn_properties_to_manifest

        props = self._run_cfn_template(
            {
                "namespace": "test-ns",
                "resources": {
                    "gpt-4": {"disabled": True, "limits": {"rpm": {"capacity": 1000}}},
                    "claude-3": {"limits": {"rpm": {"capacity": 500}}},
                },
                "entities": {
                    "vip-1": {
                        "resources": {
                            "gpt-4": {"disabled": False, "limits": {"rpm": {"capacity": 2000}}}
                        }
                    }
                },
            }
        )

        round_tripped = _cfn_properties_to_manifest(props)

        assert round_tripped["resources"]["gpt-4"]["disabled"] is True
        assert "disabled" not in round_tripped["resources"]["claude-3"]
        assert round_tripped["entities"]["vip-1"]["resources"]["gpt-4"]["disabled"] is False


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
        """_invoke_provisioner resolves namespace_id via Repository.open()."""
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
                    "zae_limiter.repository.Repository.open",
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
                    "zae_limiter.repository.Repository.open",
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

    def test_invoke_provisioner_missing_function_exits_cleanly(self):
        """A stack deployed without the provisioner gets an explanation, not a traceback."""
        yaml_content = {"namespace": "test-ns"}
        with tempfile.NamedTemporaryFile(suffix=".yaml", mode="w", delete=False) as f:
            yaml.dump(yaml_content, f)
            f.flush()

            class FakeResourceNotFoundError(Exception):
                """Stand-in for the botocore-generated client exception."""

            runner = CliRunner()
            with (
                patch("asyncio.run", side_effect=Exception("no repo")),
                patch("zae_limiter.limits_cli.boto3.client") as mock_boto3_client,
            ):
                mock_lambda = MagicMock()
                mock_lambda.exceptions.ResourceNotFoundException = FakeResourceNotFoundError
                mock_lambda.invoke.side_effect = FakeResourceNotFoundError("no such function")
                mock_boto3_client.return_value = mock_lambda

                result = runner.invoke(
                    cli,
                    ["limits", "plan", "--name", "test-app", "-f", f.name],
                )
                assert result.exit_code == 1
                assert result.exception is None or isinstance(result.exception, SystemExit)
                assert "test-app-limits-provisioner" in result.output
                assert "--no-provisioner" in result.output

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


class TestLimitsCfnTemplateSchedules:
    """`schedule` / `reset_schedule` (#222) -> CFN `Schedule` / `ResetSchedule`.

    The generator walks raw YAML dicts, so nothing here is validated by
    ``LimitDecl``; a key-name or case mistake would produce a template that
    deploys and then fails inside the provisioner Lambda, or worse, silently
    drops the schedule. Every assertion below is therefore an exact match on
    the emitted structure rather than a containment check.
    """

    def _render(self, yaml_content: dict) -> dict:
        with tempfile.NamedTemporaryFile(suffix=".yaml", mode="w", delete=False) as f:
            yaml.dump(yaml_content, f)
            f.flush()

            result = CliRunner().invoke(
                cli,
                ["limits", "cfn-template", "--name", "test-app", "-f", f.name],
            )
        assert result.exit_code == 0, result.output
        parsed: dict = yaml.safe_load(result.output)
        return parsed

    def _props(self, yaml_content: dict) -> dict:
        props: dict = self._render(yaml_content)["Resources"]["TenantLimits"]["Properties"]
        return props

    def _resource_limits(self, yaml_content: dict, resource: str = "gpt-4") -> dict:
        limits: dict = self._props(yaml_content)["Resources"][resource]["Limits"]
        return limits

    MANIFEST = {
        "namespace": "test-ns",
        "resources": {
            "gpt-4": {
                "limits": {
                    "rpm": {
                        "capacity": 1000,
                        "schedule": [
                            {
                                "cron": "* 9-17 * * MON-FRI",
                                "tz": "America/New_York",
                                "scale": 0.5,
                            },
                            {
                                "cron": "* 0-6 * * *",
                                "tz": "America/New_York",
                                "capacity": 2000,
                            },
                        ],
                    },
                    "rpd": {
                        "capacity": 10000,
                        "refill_period": 86400,
                        "reset_schedule": [{"cron": "0 0 * * *", "tz": "America/New_York"}],
                    },
                }
            }
        },
    }

    def test_emits_every_schedule_entry_in_order(self):
        """Two entries, two shapes — a table keyed on the first entry only, or
        one that reorders, fails here."""
        limits = self._resource_limits(self.MANIFEST)
        assert limits["rpm"]["Schedule"] == [
            {"Cron": "* 9-17 * * MON-FRI", "Tz": "America/New_York", "Scale": 0.5},
            {"Cron": "* 0-6 * * *", "Tz": "America/New_York", "Capacity": 2000},
        ]

    def test_emits_reset_schedule(self):
        limits = self._resource_limits(self.MANIFEST)
        assert limits["rpd"]["ResetSchedule"] == [{"Cron": "0 0 * * *", "Tz": "America/New_York"}]

    def test_schedule_and_reset_schedule_coexist_on_one_limit(self):
        """A quota may also carry a scaling schedule (ADR-137 allows `scale`
        on a reset limit — scaling a zero rate leaves it zero). Emitting one
        tuple into the other's property, or letting the second overwrite the
        first, fails here."""
        limits = self._resource_limits(
            {
                "namespace": "test-ns",
                "resources": {
                    "gpt-4": {
                        "limits": {
                            "rpd": {
                                "capacity": 10000,
                                "refill_period": 86400,
                                "schedule": [{"cron": "* * * * SAT,SUN", "scale": 0.5}],
                                "reset_schedule": [{"cron": "0 0 * * *", "tz": "UTC"}],
                            }
                        }
                    }
                },
            }
        )
        assert limits["rpd"]["Schedule"] == [{"Cron": "* * * * SAT,SUN", "Scale": 0.5}]
        assert limits["rpd"]["ResetSchedule"] == [{"Cron": "0 0 * * *", "Tz": "UTC"}]

    def test_every_entry_field_is_emitted(self):
        """All six allowlisted entry fields, including the two whose CFN
        spelling is not a naive title-case of the snake_case name."""
        limits = self._resource_limits(
            {
                "namespace": "test-ns",
                "resources": {
                    "gpt-4": {
                        "limits": {
                            "rpm": {
                                "capacity": 1000,
                                "schedule": [
                                    {
                                        "cron": "* * * * *",
                                        "tz": "Europe/Paris",
                                        "capacity": 5,
                                        "refill_amount": 6,
                                        "refill_period_seconds": 7,
                                    }
                                ],
                            }
                        }
                    }
                },
            }
        )
        assert limits["rpm"]["Schedule"] == [
            {
                "Cron": "* * * * *",
                "Tz": "Europe/Paris",
                "Capacity": 5,
                "RefillAmount": 6,
                "RefillPeriodSeconds": 7,
            }
        ]

    def test_schedules_emitted_at_system_resource_and_entity_levels(self):
        """`_limits_to_cfn` is called from three places; a change wired into
        only the resource branch passes a resource-only test."""
        props = self._props(
            {
                "namespace": "test-ns",
                "system": {
                    "limits": {
                        "rpm": {"capacity": 1, "schedule": [{"cron": "* * * * *", "scale": 2.0}]}
                    }
                },
                "resources": {
                    "gpt-4": {
                        "limits": {
                            "rpm": {
                                "capacity": 2,
                                "schedule": [{"cron": "1 * * * *", "scale": 3.0}],
                            }
                        }
                    }
                },
                "entities": {
                    "vip-1": {
                        "resources": {
                            "gpt-4": {
                                "limits": {
                                    "rpm": {
                                        "capacity": 3,
                                        "schedule": [{"cron": "2 * * * *", "scale": 4.0}],
                                    }
                                }
                            }
                        }
                    }
                },
            }
        )
        assert props["System"]["Limits"]["rpm"]["Schedule"] == [{"Cron": "* * * * *", "Scale": 2.0}]
        assert props["Resources"]["gpt-4"]["Limits"]["rpm"]["Schedule"] == [
            {"Cron": "1 * * * *", "Scale": 3.0}
        ]
        assert props["Entities"]["vip-1"]["Resources"]["gpt-4"]["Limits"]["rpm"]["Schedule"] == [
            {"Cron": "2 * * * *", "Scale": 4.0}
        ]

    def test_omits_both_properties_when_absent(self):
        """Absent means "no schedule"; an emitted empty list would make the
        template of an unscheduled manifest differ from what it was before
        schedules existed."""
        limits = self._resource_limits(
            {
                "namespace": "test-ns",
                "resources": {"gpt-4": {"limits": {"rpm": {"capacity": 1000}}}},
            }
        )
        assert limits["rpm"] == {"Capacity": 1000}

    def test_unscheduled_manifest_template_is_unchanged_by_this_feature(self):
        """The whole template, not just one limit — pins the no-schedule wire
        shape that ``LimitDecl.to_dict()`` also preserves."""
        template = self._render(
            {
                "namespace": "test-ns",
                "system": {"on_unavailable": "block", "limits": {"rpm": {"capacity": 9}}},
                "resources": {"gpt-4": {"limits": {"rpm": {"capacity": 1000}}}},
                "entities": {
                    "vip-1": {"resources": {"gpt-4": {"limits": {"rpm": {"capacity": 5}}}}}
                },
            }
        )
        assert template["Resources"]["TenantLimits"]["Properties"] == {
            "ServiceToken": {"Fn::ImportValue": "test-app-ProvisionerArn"},
            "TableName": "test-app",
            "Namespace": "test-ns",
            "System": {"OnUnavailable": "block", "Limits": {"rpm": {"Capacity": 9}}},
            "Resources": {"gpt-4": {"Limits": {"rpm": {"Capacity": 1000}}}},
            "Entities": {"vip-1": {"Resources": {"gpt-4": {"Limits": {"rpm": {"Capacity": 5}}}}}},
        }

    def test_empty_schedule_list_is_treated_as_absent(self):
        """`LimitDecl.to_dict()` emits the key only when the tuple is
        non-empty; the CFN generator has to agree or the two wire formats the
        provisioner accepts diverge for the same manifest."""
        limits = self._resource_limits(
            {
                "namespace": "test-ns",
                "resources": {
                    "gpt-4": {
                        "limits": {"rpm": {"capacity": 1000, "schedule": [], "reset_schedule": []}}
                    }
                },
            }
        )
        assert limits["rpm"] == {"Capacity": 1000}

    def test_null_schedule_is_treated_as_absent(self):
        """`schedule:` with nothing under it is YAML null, not a list —
        ``_parse_entries`` accepts it as "none", so the generator must too
        rather than raising TypeError out of a Click command."""
        limits = self._resource_limits(
            {
                "namespace": "test-ns",
                "resources": {
                    "gpt-4": {
                        "limits": {
                            "rpm": {"capacity": 1000, "schedule": None, "reset_schedule": None}
                        }
                    }
                },
            }
        )
        assert limits["rpm"] == {"Capacity": 1000}

    def test_non_list_schedule_is_a_clean_cli_error(self):
        """The manifest parser rejects this with a ValueError naming the key;
        the generator must not emit a template or dump a traceback."""
        with tempfile.NamedTemporaryFile(suffix=".yaml", mode="w", delete=False) as f:
            yaml.dump(
                {
                    "namespace": "test-ns",
                    "resources": {
                        "gpt-4": {"limits": {"rpm": {"capacity": 1, "schedule": "0 0 * * *"}}}
                    },
                },
                f,
            )
            f.flush()
            result = CliRunner().invoke(
                cli, ["limits", "cfn-template", "--name", "test-app", "-f", f.name]
            )
        assert result.exit_code != 0
        assert "schedule" in result.output
        assert "Traceback" not in result.output

    def test_non_mapping_schedule_entry_is_a_clean_cli_error(self):
        """Same contract one level down: `_parse_entries` rejects a non-mapping
        entry by index, so the generator must too rather than raising
        TypeError while subscripting a string."""
        with tempfile.NamedTemporaryFile(suffix=".yaml", mode="w", delete=False) as f:
            yaml.dump(
                {
                    "namespace": "test-ns",
                    "resources": {
                        "gpt-4": {
                            "limits": {"rpm": {"capacity": 1, "reset_schedule": ["0 0 * * *"]}}
                        }
                    },
                },
                f,
            )
            f.flush()
            result = CliRunner().invoke(
                cli, ["limits", "cfn-template", "--name", "test-app", "-f", f.name]
            )
        assert result.exit_code != 0
        assert "reset_schedule[0]" in result.output
        assert "Traceback" not in result.output

    def test_cron_stays_standard_never_compact(self):
        """CloudFormation is user-facing IaC — the compact storage encoding
        must never reach a template (project rule, design §4)."""
        template = self._render(self.MANIFEST)
        dumped = yaml.dump(template)
        limits = self._resource_limits(self.MANIFEST)
        crons = [entry["Cron"] for entry in limits["rpm"]["Schedule"]] + [
            entry["Cron"] for entry in limits["rpd"]["ResetSchedule"]
        ]
        assert crons == ["* 9-17 * * MON-FRI", "* 0-6 * * *", "0 0 * * *"]
        for cron in crons:
            assert len(cron.split()) == 5, cron
        assert "h9-17" not in dumped

    def test_pascal_case_table_covers_exactly_the_manifest_allowlist(self):
        """``_parse_entries`` validates against a strict six-key allowlist. A
        seventh CFN property, or one this table spells differently, becomes a
        ValueError inside the Lambda at deploy time — pin the two together."""
        from zae_limiter.limits_cli import _SCHEDULE_KEYS
        from zae_limiter_provisioner.manifest import _ENTRY_FIELDS, _RESET_ENTRY_FIELDS

        assert tuple(snake for snake, _ in _SCHEDULE_KEYS) == _ENTRY_FIELDS
        assert set(_RESET_ENTRY_FIELDS) <= set(_ENTRY_FIELDS)
        assert "_reset" not in dict(_SCHEDULE_KEYS)

    def test_full_round_trip_through_cfn_is_lossless(self):
        """YAML -> template -> provisioner -> ``LimitsManifest`` must land on
        exactly the manifest ``limits apply`` would have sent directly. This is
        the only assertion that fails if the two key tables drift apart."""
        from zae_limiter_provisioner.handler import _cfn_properties_to_manifest
        from zae_limiter_provisioner.manifest import LimitsManifest

        source = {
            "namespace": "test-ns",
            "system": {
                "on_unavailable": "block",
                "limits": {
                    "rpm": {
                        "capacity": 100,
                        "schedule": [
                            {"cron": "* 9-17 * * MON-FRI", "scale": 0.5},
                            {
                                "cron": "0 3 * * *",
                                "tz": "Europe/Paris",
                                "capacity": 10,
                                "refill_amount": 11,
                                "refill_period_seconds": 12,
                            },
                        ],
                    }
                },
            },
            "resources": {
                "gpt-4": {
                    "disabled": False,
                    "limits": {
                        "rpd": {
                            "capacity": 10000,
                            "refill_period": 86400,
                            "schedule": [
                                {"cron": "* * * * SAT,SUN", "tz": "UTC", "scale": 0.25},
                                {
                                    "cron": "0 0 1 * *",
                                    "tz": "America/New_York",
                                    "capacity": 50000,
                                },
                            ],
                            "reset_schedule": [{"cron": "0 0 * * *", "tz": "America/New_York"}],
                        },
                        "rpm": {"capacity": 60},
                    },
                }
            },
            "entities": {
                "vip-1": {
                    "resources": {
                        "gpt-4": {
                            "limits": {
                                "rpd": {
                                    "capacity": 99999,
                                    "reset_schedule": [{"cron": "30 4 * * MON", "tz": "UTC"}],
                                },
                                # ADR-139: the third recovery spelling, alongside
                                # the `reset_schedule` case above.
                                "session": {
                                    "capacity": 10000,
                                    "reset_after_seconds": 18000,
                                },
                            }
                        }
                    }
                }
            },
        }

        direct = LimitsManifest.from_dict(source).to_dict()
        via_cfn = LimitsManifest.from_dict(
            _cfn_properties_to_manifest(self._props(source))
        ).to_dict()
        assert via_cfn == direct


class TestLimitsCfnTemplateDurationWindow:
    """`reset_after_seconds` (ADR-139) -> CFN `ResetAfterSeconds`, mirroring
    `TestLimitsCfnTemplateSchedules` for the third recovery spelling."""

    def _render(self, yaml_content: dict) -> dict:
        with tempfile.NamedTemporaryFile(suffix=".yaml", mode="w", delete=False) as f:
            yaml.dump(yaml_content, f)
            f.flush()

            result = CliRunner().invoke(
                cli,
                ["limits", "cfn-template", "--name", "test-app", "-f", f.name],
            )
        assert result.exit_code == 0, result.output
        parsed: dict = yaml.safe_load(result.output)
        return parsed

    def _resource_limits(self, yaml_content: dict, resource: str = "claude-sonnet") -> dict:
        props = self._render(yaml_content)["Resources"]["TenantLimits"]["Properties"]
        limits: dict = props["Resources"][resource]["Limits"]
        return limits

    def test_emits_reset_after_seconds(self):
        limits = self._resource_limits(
            {
                "namespace": "test-ns",
                "resources": {
                    "claude-sonnet": {
                        "limits": {"session": {"capacity": 10000, "reset_after_seconds": 18000}}
                    }
                },
            }
        )
        assert limits["session"] == {"Capacity": 10000, "ResetAfterSeconds": 18000}

    def test_omits_reset_after_seconds_when_absent(self):
        """Absent means "no window"; an ordinary rate limit's template is
        unchanged by this feature."""
        limits = self._resource_limits(
            {
                "namespace": "test-ns",
                "resources": {"claude-sonnet": {"limits": {"rpm": {"capacity": 1000}}}},
            }
        )
        assert limits["rpm"] == {"Capacity": 1000}
        assert "ResetAfterSeconds" not in limits["rpm"]
