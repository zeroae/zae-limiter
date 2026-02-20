"""Tests for the provisioner Lambda handler."""

from unittest.mock import MagicMock, patch

from zae_limiter_provisioner.handler import on_event


@patch("zae_limiter_provisioner.handler.urllib.request.urlopen")
@patch("zae_limiter_provisioner.applier.boto3")
@patch("zae_limiter_provisioner.handler.boto3")
class TestProvisionerHandler:
    """Tests for the provisioner Lambda handler."""

    def _setup_client(self, mock_handler_boto3, mock_applier_boto3, get_item_return=None):
        """Set up shared mock client for both handler and applier boto3."""
        mock_client = MagicMock()
        mock_client.get_item.return_value = get_item_return or {}
        mock_handler_boto3.client.return_value = mock_client
        mock_applier_boto3.client.return_value = mock_client
        return mock_client

    def test_plan_action_returns_changes(
        self, mock_handler_boto3, mock_applier_boto3, mock_urlopen
    ):
        """Plan action computes diff without applying."""
        mock_client = self._setup_client(mock_handler_boto3, mock_applier_boto3)

        event = {
            "action": "plan",
            "table_name": "test-table",
            "namespace_id": "ns123",
            "manifest": {
                "namespace": "test-ns",
                "system": {"limits": {"rpm": {"capacity": 1000}}},
            },
        }
        result = on_event(event, MagicMock())
        assert result["status"] == "planned"
        assert len(result["changes"]) > 0
        # Plan should NOT write to DynamoDB
        mock_client.put_item.assert_not_called()

    def test_apply_action_applies_and_returns(
        self, mock_handler_boto3, mock_applier_boto3, mock_urlopen
    ):
        """Apply action applies changes and updates state."""
        mock_client = self._setup_client(mock_handler_boto3, mock_applier_boto3)

        event = {
            "action": "apply",
            "table_name": "test-table",
            "namespace_id": "ns123",
            "manifest": {
                "namespace": "test-ns",
                "resources": {"gpt-4": {"limits": {"rpm": {"capacity": 1000}}}},
            },
        }
        result = on_event(event, MagicMock())
        assert result["status"] == "applied"
        assert "changes" in result
        assert result["created"] == 1
        # Should write config + state
        assert mock_client.put_item.call_count >= 2

    def test_plan_no_changes(self, mock_handler_boto3, mock_applier_boto3, mock_urlopen):
        """Plan with no changes returns empty list."""
        self._setup_client(mock_handler_boto3, mock_applier_boto3)

        event = {
            "action": "plan",
            "table_name": "test-table",
            "namespace_id": "ns123",
            "manifest": {"namespace": "test-ns"},
        }
        result = on_event(event, MagicMock())
        assert result["status"] == "planned"
        assert result["changes"] == []

    def test_cfn_create_event(self, mock_handler_boto3, mock_applier_boto3, mock_urlopen):
        """CloudFormation Create event applies the manifest."""
        self._setup_client(mock_handler_boto3, mock_applier_boto3)

        event = {
            "RequestType": "Create",
            "ResourceProperties": {
                "ServiceToken": "arn:aws:lambda:us-east-1:123:function:test",
                "TableName": "test-table",
                "Namespace": "test-ns",
                "NamespaceId": "ns123",
                "System": {"Limits": {"rpm": {"Capacity": 1000}}},
            },
            "ResponseURL": "https://cfn-response.example.com",
            "StackId": "arn:aws:cloudformation:us-east-1:123:stack/test/guid",
            "RequestId": "test-request-id",
            "LogicalResourceId": "TenantLimits",
        }
        result = on_event(event, MagicMock())
        assert result["status"] == "applied"
        assert any(c["action"] == "create" and c["level"] == "system" for c in result["changes"])

    def test_cfn_delete_event_clears_all(
        self, mock_handler_boto3, mock_applier_boto3, mock_urlopen
    ):
        """CloudFormation Delete event applies empty manifest (deletes all managed)."""
        self._setup_client(
            mock_handler_boto3,
            mock_applier_boto3,
            get_item_return={
                "Item": {
                    "managed_system": {"BOOL": True},
                    "managed_resources": {"L": [{"S": "gpt-4"}]},
                    "managed_entities": {"M": {}},
                }
            },
        )

        event = {
            "RequestType": "Delete",
            "ResourceProperties": {
                "ServiceToken": "arn:aws:lambda:us-east-1:123:function:test",
                "TableName": "test-table",
                "Namespace": "test-ns",
                "NamespaceId": "ns123",
            },
            "ResponseURL": "https://cfn-response.example.com",
            "StackId": "arn:aws:cloudformation:us-east-1:123:stack/test/guid",
            "RequestId": "test-request-id",
            "LogicalResourceId": "TenantLimits",
        }
        result = on_event(event, MagicMock())
        assert result["status"] == "applied"
        # Should delete system and gpt-4 resource
        delete_actions = [c for c in result["changes"] if c["action"] == "delete"]
        assert len(delete_actions) == 2

    def test_cfn_update_event(self, mock_handler_boto3, mock_applier_boto3, mock_urlopen):
        """CloudFormation Update event diffs against previous state."""
        self._setup_client(
            mock_handler_boto3,
            mock_applier_boto3,
            get_item_return={
                "Item": {
                    "managed_system": {"BOOL": False},
                    "managed_resources": {"L": [{"S": "gpt-4"}]},
                    "managed_entities": {"M": {}},
                }
            },
        )

        event = {
            "RequestType": "Update",
            "ResourceProperties": {
                "ServiceToken": "arn:aws:lambda:us-east-1:123:function:test",
                "TableName": "test-table",
                "Namespace": "test-ns",
                "NamespaceId": "ns123",
                "Resources": {
                    "gpt-4": {"Limits": {"rpm": {"Capacity": 2000}}},
                    "claude-3": {"Limits": {"tpm": {"Capacity": 100000}}},
                },
            },
            "ResponseURL": "https://cfn-response.example.com",
            "StackId": "arn:aws:cloudformation:us-east-1:123:stack/test/guid",
            "RequestId": "test-request-id",
            "LogicalResourceId": "TenantLimits",
        }
        result = on_event(event, MagicMock())
        assert result["status"] == "applied"
        actions = {(c["level"], c["target"], c["action"]) for c in result["changes"]}
        assert ("resource", "gpt-4", "update") in actions
        assert ("resource", "claude-3", "create") in actions
