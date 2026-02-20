"""Tests for the provisioner applier."""

from unittest.mock import MagicMock

from zae_limiter_provisioner.applier import apply_changes
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
                                "burst": 1000,
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
                                "burst": 500,
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
                                "burst": 50000,
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
                                "burst": 1000,
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
