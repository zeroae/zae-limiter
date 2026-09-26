"""Tests for the provisioner applier."""

from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

from zae_limiter.exceptions import VersionMismatchError
from zae_limiter_provisioner.applier import apply_changes, require_reset_after_readers
from zae_limiter_provisioner.differ import Change


class TestApplyChanges:
    """Tests for applying changes to DynamoDB via Repository-equivalent operations."""

    def _make_client(self) -> MagicMock:
        """Create a mock boto3 DynamoDB client."""
        return MagicMock()

    def test_apply_empty_changes(self):
        """Empty change list produces zero-change result."""
        result = apply_changes([], table_name="test", namespace_id="ns123")
        assert result.created == 0
        assert result.updated == 0
        assert result.deleted == 0
        assert result.errors == []

    def test_apply_create_system(self):
        """Create system defaults calls put_item with correct keys."""
        client = self._make_client()
        result = apply_changes(
            [
                Change(
                    action="create",
                    level="system",
                    target=None,
                    data={
                        "limits": {
                            "rpm": {
                                "capacity": 1000,
                                "refill_amount": 1000,
                                "refill_period": 60,
                            }
                        }
                    },
                )
            ],
            table_name="test",
            namespace_id="ns123",
            client=client,
        )
        assert result.created == 1
        client.put_item.assert_called_once()
        item = client.put_item.call_args[1]["Item"]
        assert item["PK"]["S"] == "ns123/SYSTEM#"
        assert item["SK"]["S"] == "#CONFIG"
        assert item["l_rpm_cp"]["N"] == "1000"

    def test_apply_delete_resource(self):
        """Delete resource defaults calls delete_item."""
        client = self._make_client()
        result = apply_changes(
            [Change(action="delete", level="resource", target="gpt-4")],
            table_name="test",
            namespace_id="ns123",
            client=client,
        )
        assert result.deleted == 1
        client.delete_item.assert_called_once()
        key = client.delete_item.call_args[1]["Key"]
        assert key["PK"]["S"] == "ns123/RESOURCE#gpt-4"
        assert key["SK"]["S"] == "#CONFIG"

    def test_apply_create_entity(self):
        """Create entity limits calls put_item with entity/resource keys."""
        client = self._make_client()
        result = apply_changes(
            [
                Change(
                    action="create",
                    level="entity",
                    target="user-1/gpt-4",
                    data={
                        "limits": {
                            "rpm": {
                                "capacity": 500,
                                "refill_amount": 500,
                                "refill_period": 60,
                            }
                        }
                    },
                )
            ],
            table_name="test",
            namespace_id="ns123",
            client=client,
        )
        assert result.created == 1
        item = client.put_item.call_args[1]["Item"]
        assert item["PK"]["S"] == "ns123/ENTITY#user-1"
        assert item["SK"]["S"] == "#CONFIG#gpt-4"
        assert item["entity_id"]["S"] == "user-1"
        assert item["resource"]["S"] == "gpt-4"

    def test_apply_update_resource(self):
        """Update resource counts as updated, not created."""
        client = self._make_client()
        result = apply_changes(
            [
                Change(
                    action="update",
                    level="resource",
                    target="gpt-4",
                    data={
                        "limits": {
                            "tpm": {
                                "capacity": 50000,
                                "refill_amount": 50000,
                                "refill_period": 60,
                            }
                        }
                    },
                )
            ],
            table_name="test",
            namespace_id="ns123",
            client=client,
        )
        assert result.updated == 1
        assert result.created == 0

    def test_apply_mixed_changes(self):
        """Mixed create/update/delete produces correct counts."""
        client = self._make_client()
        changes = [
            Change(
                action="create",
                level="system",
                target=None,
                data={
                    "limits": {
                        "rpm": {
                            "capacity": 1000,
                            "burst": 1000,
                            "refill_amount": 1000,
                            "refill_period": 60,
                        }
                    }
                },
            ),
            Change(
                action="update",
                level="resource",
                target="gpt-4",
                data={
                    "limits": {
                        "tpm": {
                            "capacity": 50000,
                            "burst": 50000,
                            "refill_amount": 50000,
                            "refill_period": 60,
                        }
                    }
                },
            ),
            Change(action="delete", level="resource", target="claude-3"),
            Change(action="delete", level="entity", target="user-1/gpt-4"),
        ]
        result = apply_changes(changes, table_name="test", namespace_id="ns123", client=client)
        assert result.created == 1
        assert result.updated == 1
        assert result.deleted == 2

    def test_apply_system_with_on_unavailable(self):
        """System create includes on_unavailable in the item."""
        client = self._make_client()
        apply_changes(
            [
                Change(
                    action="create",
                    level="system",
                    target=None,
                    data={
                        "on_unavailable": "allow",
                        "limits": {
                            "rpm": {
                                "capacity": 1000,
                                "refill_amount": 1000,
                                "refill_period": 60,
                            }
                        },
                    },
                )
            ],
            table_name="test",
            namespace_id="ns123",
            client=client,
        )
        item = client.put_item.call_args[1]["Item"]
        assert item["on_unavailable"]["S"] == "allow"

    def test_apply_create_resource_with_disabled_true(self):
        """Resource create with disabled=True writes a BOOL attribute."""
        client = self._make_client()
        apply_changes(
            [
                Change(
                    action="create",
                    level="resource",
                    target="gpt-4",
                    data={
                        "disabled": True,
                        "limits": {
                            "rpm": {"capacity": 10, "refill_amount": 10, "refill_period": 60}
                        },
                    },
                )
            ],
            table_name="test",
            namespace_id="ns123",
            client=client,
        )
        item = client.put_item.call_args[1]["Item"]
        assert item["disabled"] == {"BOOL": True}

    def test_apply_create_entity_with_disabled_false(self):
        """Entity create with disabled=False writes a BOOL(False) attribute (carve-out)."""
        client = self._make_client()
        apply_changes(
            [
                Change(
                    action="create",
                    level="entity",
                    target="vip-1/gpt-4",
                    data={
                        "disabled": False,
                        "limits": {
                            "rpm": {"capacity": 10, "refill_amount": 10, "refill_period": 60}
                        },
                    },
                )
            ],
            table_name="test",
            namespace_id="ns123",
            client=client,
        )
        item = client.put_item.call_args[1]["Item"]
        assert item["disabled"] == {"BOOL": False}

    def test_apply_create_resource_without_disabled_omits_attribute(self):
        """Resource create with no `disabled` key in the manifest omits the attribute."""
        client = self._make_client()
        apply_changes(
            [
                Change(
                    action="create",
                    level="resource",
                    target="gpt-4",
                    data={
                        "limits": {
                            "rpm": {"capacity": 10, "refill_amount": 10, "refill_period": 60}
                        }
                    },
                )
            ],
            table_name="test",
            namespace_id="ns123",
            client=client,
        )
        item = client.put_item.call_args[1]["Item"]
        assert "disabled" not in item

    def test_apply_error_collected(self):
        """Errors from individual operations are collected, not raised."""
        client = self._make_client()
        client.put_item.side_effect = Exception("DynamoDB error")
        result = apply_changes(
            [
                Change(
                    action="create",
                    level="system",
                    target=None,
                    data={"limits": {}},
                )
            ],
            table_name="test",
            namespace_id="ns123",
            client=client,
        )
        assert len(result.errors) == 1
        assert "DynamoDB error" in result.errors[0]


class TestScheduleReachesTheConfigItem:
    """A manifest schedule must be stored, not merely fanned out (#222).

    `LimitDecl` learned to parse `schedule` / `reset_schedule` in #543, but the
    applier wrote cp/ra/rp and nothing else, so the schedule survived only as
    long as the bucket items the fan-out stamped. The next `acquire()` that
    recreated a bucket resolved its limits from config, found no schedule, and
    created the bucket unscheduled — silently, with no error anywhere.
    """

    BIZ = [{"cron": "* 9-17 * * MON-FRI", "tz": "America/New_York", "scale": 0.5}]
    MIDNIGHT = [{"cron": "0 0 * * *", "tz": "America/New_York"}]

    @staticmethod
    def _item(limits):
        client = MagicMock()
        result = apply_changes(
            [
                Change(
                    action="update", level="entity", target="user-1/gpt-4", data={"limits": limits}
                )
            ],
            table_name="test",
            namespace_id="ns123",
            client=client,
        )
        assert result.errors == []
        return client.put_item.call_args.kwargs["Item"]

    def test_a_parameter_schedule_is_stored_compact(self):
        item = self._item(
            {
                "rpm": {
                    "capacity": 1000,
                    "refill_amount": 1000,
                    "refill_period": 60,
                    "schedule": self.BIZ,
                }
            }
        )
        assert item["l_rpm_sched"] == {"S": "1h9-17w1-5s500"}
        assert item["sched_tz"] == {"S": "America/New_York"}
        # cp/ra stay the BASE params; the schedule applies on top (§2.1).
        assert item["l_rpm_cp"] == {"N": "1000"}

    def test_a_reset_schedule_is_stored_compact(self):
        item = self._item(
            {
                "rpd": {
                    "capacity": 10000,
                    "refill_amount": 0,
                    "refill_period": 86400,
                    "reset_schedule": self.MIDNIGHT,
                }
            }
        )
        assert item["l_rpd_rsched"] == {"S": "1m0h0"}
        assert item["sched_tz"] == {"S": "America/New_York"}
        assert "l_rpd_sched" not in item

    def test_an_unscheduled_limit_writes_neither_attribute(self):
        """Absence is how "no schedule" is stored; PutItem is full-replace, so
        an omitted attribute is also how a schedule is removed."""
        item = self._item({"rpm": {"capacity": 1000, "refill_amount": 1000, "refill_period": 60}})
        assert "l_rpm_sched" not in item
        assert "l_rpm_rsched" not in item
        assert "sched_tz" not in item

    def test_only_the_scheduled_limit_on_a_shared_item_is_stamped(self):
        """N limits per config item: `sched_tz` is item-level, `sched` is not."""
        item = self._item(
            {
                "rpm": {
                    "capacity": 1000,
                    "refill_amount": 1000,
                    "refill_period": 60,
                    "schedule": self.BIZ,
                },
                "tpm": {"capacity": 50, "refill_amount": 50, "refill_period": 60},
            }
        )
        assert item["l_rpm_sched"] == {"S": "1h9-17w1-5s500"}
        assert "l_tpm_sched" not in item
        assert item["sched_tz"] == {"S": "America/New_York"}

    def test_limits_disagreeing_on_a_timezone_are_reported_not_written(self):
        """One item, one `sched_tz`. Keeping the first limit's zone would
        reinterpret the second limit's cron in the wrong one."""
        client = MagicMock()
        result = apply_changes(
            [
                Change(
                    action="update",
                    level="resource",
                    target="gpt-4",
                    data={
                        "limits": {
                            "rpm": {
                                "capacity": 1,
                                "refill_amount": 1,
                                "refill_period": 60,
                                "schedule": self.BIZ,
                            },
                            "tpm": {
                                "capacity": 1,
                                "refill_amount": 1,
                                "refill_period": 60,
                                "schedule": [
                                    {"cron": "* 9-17 * * *", "tz": "Europe/London", "scale": 0.5}
                                ],
                            },
                        }
                    },
                )
            ],
            table_name="test",
            namespace_id="ns123",
            client=client,
        )
        assert len(result.errors) == 1
        assert "timezone" in result.errors[0]
        client.put_item.assert_not_called()

    def test_the_system_level_carries_schedules_too(self):
        client = MagicMock()
        apply_changes(
            [
                Change(
                    action="create",
                    level="system",
                    target=None,
                    data={
                        "limits": {
                            "rpm": {
                                "capacity": 1000,
                                "refill_amount": 1000,
                                "refill_period": 60,
                                "schedule": self.BIZ,
                            }
                        }
                    },
                )
            ],
            table_name="test",
            namespace_id="ns123",
            client=client,
        )
        item = client.put_item.call_args.kwargs["Item"]
        assert item["l_rpm_sched"] == {"S": "1h9-17w1-5s500"}


class TestDurationWindowReachesTheConfigItem:
    """`reset_after_seconds` (ADR-139) must reach `l_{name}_rsa`, mirroring
    `TestScheduleReachesTheConfigItem` for the third recovery spelling."""

    @staticmethod
    def _item(limits, level="entity", target="user-1/gpt-4"):
        client = MagicMock()
        result = apply_changes(
            [Change(action="update", level=level, target=target, data={"limits": limits})],
            table_name="test",
            namespace_id="ns123",
            client=client,
        )
        assert result.errors == []
        return client.put_item.call_args.kwargs["Item"]

    def test_a_duration_window_is_stored(self):
        item = self._item(
            {
                "session": {
                    "capacity": 10_000,
                    "refill_amount": 0,
                    "refill_period": 1,
                    "reset_after_seconds": 18_000,
                }
            }
        )
        assert item["l_session_rsa"] == {"N": "18000"}
        # cp/ra/rp stay the base params.
        assert item["l_session_cp"] == {"N": "10000"}
        assert item["l_session_ra"] == {"N": "0"}

    def test_a_limit_without_a_window_writes_no_rsa_attribute(self):
        item = self._item({"rpm": {"capacity": 1000, "refill_amount": 1000, "refill_period": 60}})
        assert "l_rpm_rsa" not in item

    def test_only_the_windowed_limit_on_a_shared_item_is_stamped(self):
        item = self._item(
            {
                "session": {
                    "capacity": 10_000,
                    "refill_amount": 0,
                    "refill_period": 1,
                    "reset_after_seconds": 18_000,
                },
                "rpm": {"capacity": 1000, "refill_amount": 1000, "refill_period": 60},
            }
        )
        assert item["l_session_rsa"] == {"N": "18000"}
        assert "l_rpm_rsa" not in item

    def test_the_resource_level_carries_a_duration_window_too(self):
        item = self._item(
            {
                "session": {
                    "capacity": 10_000,
                    "refill_amount": 0,
                    "refill_period": 1,
                    "reset_after_seconds": 18_000,
                }
            },
            level="resource",
            target="claude-sonnet",
        )
        assert item["l_session_rsa"] == {"N": "18000"}


class TestResetAfterVersionGate:
    """The provisioner refuses a ``reset_after`` limit the stack cannot read (#638 A).

    Moto-backed: the gate's consistent read and conditional ratchet are
    DynamoDB semantics, not call shapes.
    """

    TABLE = "prov-gate"
    SESSION = {
        "capacity": 10,
        "refill_amount": 0,
        "refill_period": 1,
        "reset_after_seconds": 18_000,
    }
    RPM = {"capacity": 100, "refill_amount": 100, "refill_period": 60}

    @pytest.fixture
    def client(self, mock_dynamodb):
        import boto3

        from zae_limiter.sync_repository import SyncRepository

        setup = SyncRepository(name=self.TABLE, region="us-east-1", _skip_deprecation_warning=True)
        setup.create_table()
        setup.close()
        return boto3.client("dynamodb", region_name="us-east-1")

    def _stamp(self, client, lambda_version, client_min_version="0.0.0"):
        item = {
            "PK": {"S": "_/SYSTEM#"},
            "SK": {"S": "#VERSION"},
            "schema_version": {"S": "0.10.0"},
            "client_min_version": {"S": client_min_version},
        }
        if lambda_version is not None:
            item["lambda_version"] = {"S": lambda_version}
        client.put_item(TableName=self.TABLE, Item=item)

    def _record(self, client):
        return client.get_item(
            TableName=self.TABLE, Key={"PK": {"S": "_/SYSTEM#"}, "SK": {"S": "#VERSION"}}
        )["Item"]

    def _changes(self, limits, action="create", level="resource", target="gpt-4"):
        return [Change(action=action, level=level, target=target, data={"limits": limits})]

    def test_refused_while_the_lambdas_predate_reset_after(self, client):
        self._stamp(client, "0.14.0")
        with patch("zae_limiter_provisioner.applier.__version__", "0.15.0"):
            with pytest.raises(VersionMismatchError) as exc_info:
                require_reset_after_readers(
                    self._changes({"rpm": self.RPM, "session": self.SESSION}),
                    self.TABLE,
                    client=client,
                )
        assert "zae-limiter upgrade" in str(exc_info.value)
        assert self._record(client)["client_min_version"]["S"] == "0.0.0"

    @pytest.mark.parametrize("level,target", [("system", None), ("entity", "u1/gpt-4")])
    @pytest.mark.parametrize("action", ["create", "update"])
    def test_every_level_and_write_action_is_gated(self, client, level, target, action):
        self._stamp(client, "0.14.0")
        with patch("zae_limiter_provisioner.applier.__version__", "0.15.0"):
            with pytest.raises(VersionMismatchError):
                require_reset_after_readers(
                    self._changes({"session": self.SESSION}, action, level, target),
                    self.TABLE,
                    client=client,
                )

    @pytest.mark.parametrize("lambda_version", ["0.15.0", "0.16.1"])
    def test_admitted_and_ratcheted_once_the_lambdas_read_it(self, client, lambda_version):
        self._stamp(client, lambda_version)
        with patch("zae_limiter_provisioner.applier.__version__", "0.16.1"):
            require_reset_after_readers(
                self._changes({"session": self.SESSION}), self.TABLE, client=client
            )
        assert self._record(client)["client_min_version"]["S"] == "0.15.0"

    def test_the_ratchet_never_lowers_a_higher_minimum(self, client):
        self._stamp(client, "0.17.0", client_min_version="0.16.0")
        with patch("zae_limiter_provisioner.applier.__version__", "0.17.0"):
            require_reset_after_readers(
                self._changes({"session": self.SESSION}), self.TABLE, client=client
            )
        assert self._record(client)["client_min_version"]["S"] == "0.16.0"

    def test_the_ratchet_initializes_a_minimum_the_record_lacks(self, client):
        self._stamp(client, "0.15.0")
        client.update_item(
            TableName=self.TABLE,
            Key={"PK": {"S": "_/SYSTEM#"}, "SK": {"S": "#VERSION"}},
            UpdateExpression="REMOVE client_min_version",
        )
        with patch("zae_limiter_provisioner.applier.__version__", "0.15.0"):
            require_reset_after_readers(
                self._changes({"session": self.SESSION}), self.TABLE, client=client
            )
        assert self._record(client)["client_min_version"]["S"] == "0.15.0"

    def test_a_missing_version_record_fails_closed(self, client):
        with pytest.raises(VersionMismatchError, match="no version record"):
            require_reset_after_readers(
                self._changes({"session": self.SESSION}), self.TABLE, client=client
            )

    def test_the_version_read_is_strongly_consistent(self, client):
        self._stamp(client, "0.15.0")
        spy = MagicMock(wraps=client)
        with patch("zae_limiter_provisioner.applier.__version__", "0.15.0"):
            require_reset_after_readers(
                self._changes({"session": self.SESSION}), self.TABLE, client=spy
            )
        assert spy.get_item.call_args.kwargs["ConsistentRead"] is True

    def test_no_read_without_reset_after(self):
        """Deletes and plain limits cost nothing — not even a client."""
        client = MagicMock()
        require_reset_after_readers(
            [
                *self._changes({"rpm": self.RPM}),
                Change(action="delete", level="resource", target="old"),
            ],
            self.TABLE,
            client=client,
        )
        client.get_item.assert_not_called()

    def test_default_client_is_boto3(self):
        with patch("zae_limiter_provisioner.applier.boto3") as mock_boto3:
            mock_boto3.client.return_value.get_item.return_value = {}
            with pytest.raises(VersionMismatchError):
                require_reset_after_readers(self._changes({"session": self.SESSION}), self.TABLE)
        mock_boto3.client.assert_called_once_with("dynamodb")

    def test_a_lost_ratchet_race_re_reads(self, client):
        self._stamp(client, "0.16.0")
        spy = MagicMock(wraps=client)

        def racing(**kwargs):
            # Another writer raises the minimum past ours first.
            client.update_item(
                TableName=self.TABLE,
                Key=kwargs["Key"],
                UpdateExpression="SET client_min_version = :v",
                ExpressionAttributeValues={":v": {"S": "0.16.0"}},
            )
            raise ClientError(
                {"Error": {"Code": "ConditionalCheckFailedException", "Message": "x"}},
                "UpdateItem",
            )

        spy.update_item.side_effect = racing
        with patch("zae_limiter_provisioner.applier.__version__", "0.16.0"):
            require_reset_after_readers(
                self._changes({"session": self.SESSION}), self.TABLE, client=spy
            )
        assert spy.update_item.call_count == 1
        assert self._record(client)["client_min_version"]["S"] == "0.16.0"

    def test_a_ratchet_that_keeps_losing_raises(self, client):
        self._stamp(client, "0.15.0")
        spy = MagicMock(wraps=client)
        spy.update_item.side_effect = ClientError(
            {"Error": {"Code": "ConditionalCheckFailedException", "Message": "x"}},
            "UpdateItem",
        )
        with patch("zae_limiter_provisioner.applier.__version__", "0.15.0"):
            with pytest.raises(ClientError):
                require_reset_after_readers(
                    self._changes({"session": self.SESSION}), self.TABLE, client=spy
                )
        assert spy.update_item.call_count == 3

    def test_a_ratchet_failure_other_than_a_race_propagates(self, client):
        self._stamp(client, "0.15.0")
        spy = MagicMock(wraps=client)
        spy.update_item.side_effect = ClientError(
            {"Error": {"Code": "InternalServerError", "Message": "x"}}, "UpdateItem"
        )
        with patch("zae_limiter_provisioner.applier.__version__", "0.15.0"):
            with pytest.raises(ClientError, match="InternalServerError"):
                require_reset_after_readers(
                    self._changes({"session": self.SESSION}), self.TABLE, client=spy
                )
