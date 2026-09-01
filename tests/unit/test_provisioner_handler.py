"""Tests for the provisioner Lambda handler."""

from unittest.mock import MagicMock, patch

from zae_limiter.schema import DEFAULT_RESOURCE, pk_entity, sk_config
from zae_limiter_provisioner.handler import _cfn_properties_to_manifest, on_event


def _entity_default_get_item(namespace_id: str, entity_id: str, value: bool):
    """Build a `client.get_item` side_effect where only the entity's
    `_default_` config item is set, to `value`. Every other key (a specific
    resource's own entity config, or a resource-level config) resolves to
    "no Item", simulating that no per-resource override exists."""
    target_key = (pk_entity(namespace_id, entity_id), sk_config(DEFAULT_RESOURCE))

    def _get_item(*args, **kwargs):
        raw_key = kwargs["Key"]
        key = (raw_key["PK"]["S"], raw_key["SK"]["S"])
        if key == target_key:
            return {"Item": {"disabled": {"BOOL": value}}}
        return {}

    return _get_item


class TestCfnPropertiesToManifestDisabled:
    """Tests for the `Disabled` CFN property -> manifest `disabled` tri-state carry-through.

    A silent drop here is the worst failure mode for a kill switch: the operator's
    template update succeeds but nothing gets disabled. These tests pin the fix.
    """

    def test_resource_disabled_true_carried_through(self):
        manifest = _cfn_properties_to_manifest(
            {
                "Namespace": "test-ns",
                "Resources": {"gpt-4": {"Disabled": True, "Limits": {"rpm": {"Capacity": 1000}}}},
            }
        )
        assert manifest["resources"]["gpt-4"]["disabled"] is True

    def test_resource_disabled_absent_omitted(self):
        """No `Disabled` key in CFN properties means "inherit" — must not appear at all."""
        manifest = _cfn_properties_to_manifest(
            {
                "Namespace": "test-ns",
                "Resources": {"gpt-4": {"Limits": {"rpm": {"Capacity": 1000}}}},
            }
        )
        assert "disabled" not in manifest["resources"]["gpt-4"]

    def test_entity_resource_disabled_false_carried_through(self):
        """False is the carve-out value — it must round-trip, not be coerced or dropped."""
        manifest = _cfn_properties_to_manifest(
            {
                "Namespace": "test-ns",
                "Entities": {
                    "vip-1": {
                        "Resources": {
                            "gpt-4": {
                                "Disabled": False,
                                "Limits": {"rpm": {"Capacity": 1000}},
                            }
                        }
                    }
                },
            }
        )
        entity_decl = manifest["entities"]["vip-1"]["resources"]["gpt-4"]
        assert entity_decl["disabled"] is False

    def test_entity_resource_disabled_absent_omitted(self):
        manifest = _cfn_properties_to_manifest(
            {
                "Namespace": "test-ns",
                "Entities": {
                    "vip-1": {"Resources": {"gpt-4": {"Limits": {"rpm": {"Capacity": 1000}}}}}
                },
            }
        )
        assert "disabled" not in manifest["entities"]["vip-1"]["resources"]["gpt-4"]

    def test_system_disabled_never_carried_through(self):
        """System-level disable is out of scope (ADR-125) — no such CFN property exists to carry."""
        manifest = _cfn_properties_to_manifest(
            {
                "Namespace": "test-ns",
                "System": {"Limits": {"rpm": {"Capacity": 1000}}},
            }
        )
        assert "disabled" not in manifest["system"]


@patch("zae_limiter_provisioner.handler.urllib.request.urlopen")
@patch("zae_limiter_provisioner.applier.boto3")
@patch("zae_limiter_provisioner.handler.boto3")
class TestProvisionerHandler:
    """Tests for the provisioner Lambda handler."""

    def _setup_client(self, mock_handler_boto3, mock_applier_boto3, get_item_return=None):
        """Set up shared mock client for both handler and applier boto3."""
        mock_client = MagicMock()
        mock_client.get_item.return_value = get_item_return or {}
        # Default to "no buckets discovered" so fan-out (now unconditional on every
        # create/update, per the fix below) doesn't hang: an unconfigured MagicMock
        # response is truthy for `LastEvaluatedKey`, which would loop forever.
        mock_client.query.return_value = {"Items": []}
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

    def test_apply_with_disabled_resource_fans_out(
        self, mock_handler_boto3, mock_applier_boto3, mock_urlopen
    ):
        """A resource-level `disabled` key in the manifest triggers bucket fan-out."""
        mock_client = self._setup_client(mock_handler_boto3, mock_applier_boto3)
        mock_client.query.return_value = {"Items": [{"PK": {"S": "ns123/BUCKET#user-1#gpt-4#0"}}]}

        event = {
            "action": "apply",
            "table_name": "test-table",
            "namespace_id": "ns123",
            "manifest": {
                "namespace": "test-ns",
                "resources": {"gpt-4": {"disabled": True, "limits": {"rpm": {"capacity": 1000}}}},
            },
        }
        result = on_event(event, MagicMock())
        assert result["status"] == "applied"

        # GSI2 query for the resource fan-out, plus a stamp UpdateItem call.
        mock_client.query.assert_called()
        query_kwargs = mock_client.query.call_args.kwargs
        assert query_kwargs["IndexName"] == "GSI2"
        assert query_kwargs["ExpressionAttributeValues"][":pk"] == {"S": "ns123/RESOURCE#gpt-4"}
        mock_client.update_item.assert_called_once()
        assert mock_client.update_item.call_args.kwargs["Key"]["PK"] == {
            "S": "ns123/BUCKET#user-1#gpt-4#0"
        }

    def test_apply_without_disabled_key_still_fans_out_as_not_disabled(
        self, mock_handler_boto3, mock_applier_boto3, mock_urlopen
    ):
        """A manifest with no `disabled` key still fans out (as `disabled=False`).

        Fan-out is unconditional on every create/update now (not gated on the
        `disabled` key being present) — see `_fanout_disabled_changes`'s
        docstring for why gating on key presence was the Finding 1 bug. With
        no buckets discovered (the default empty query response), the fan-out
        is a no-op: no `update_item` call, but the discovery `query` still runs.
        """
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
        on_event(event, MagicMock())

        mock_client.query.assert_called()
        mock_client.update_item.assert_not_called()

    def test_reapply_without_disabled_line_unstamps_previously_disabled_buckets(
        self, mock_handler_boto3, mock_applier_boto3, mock_urlopen
    ):
        """Deleting the `disabled: true` line and re-applying must un-stamp buckets.

        Regression test for Finding 1 (CRITICAL): the old fan-out selection,
        `candidates = [c for c in changes if c.data and "disabled" in c.data]`,
        skipped fan-out entirely once `disabled` was removed from the YAML,
        because `ResourceDecl.to_dict()` omits the key when it is `None`. The
        config item lost `disabled` correctly (PutItem replaces the whole
        item), but every existing bucket kept its `disabled: true` stamp and
        `ResourceDisabled` kept firing off the bucket alone, with nothing in
        the manifest or the config item saying so. This must fail against the
        pre-fix filter, which never issues the second apply's `update_item`
        call at all.
        """
        mock_client = self._setup_client(mock_handler_boto3, mock_applier_boto3)
        mock_client.query.return_value = {"Items": [{"PK": {"S": "ns123/BUCKET#user-1#gpt-4#0"}}]}

        # First apply: `disabled: true` stamps the bucket.
        disable_event = {
            "action": "apply",
            "table_name": "test-table",
            "namespace_id": "ns123",
            "manifest": {
                "namespace": "test-ns",
                "resources": {"gpt-4": {"disabled": True, "limits": {"rpm": {"capacity": 1000}}}},
            },
        }
        on_event(disable_event, MagicMock())
        mock_client.update_item.assert_called_once()
        assert (
            mock_client.update_item.call_args.kwargs["UpdateExpression"] == "SET #disabled = :true"
        )

        mock_client.update_item.reset_mock()

        # Second apply: the operator deletes the `disabled: true` line and
        # re-applies. The manifest dict now has no `disabled` key at all.
        reenable_event = {
            "action": "apply",
            "table_name": "test-table",
            "namespace_id": "ns123",
            "manifest": {
                "namespace": "test-ns",
                "resources": {"gpt-4": {"limits": {"rpm": {"capacity": 1000}}}},
            },
        }
        on_event(reenable_event, MagicMock())

        mock_client.update_item.assert_called_once()
        assert mock_client.update_item.call_args.kwargs["UpdateExpression"] == "REMOVE #disabled"

    def test_apply_disabled_resource_and_entity_fans_out_resource_first(
        self, mock_handler_boto3, mock_applier_boto3, mock_urlopen
    ):
        """Resource-level fan-out runs before entity-level, so a carve-out wins (ADR-125)."""
        mock_client = self._setup_client(mock_handler_boto3, mock_applier_boto3)
        # Both fan-out queries return the same single discovered bucket so we
        # can inspect update_item call ordering directly.
        mock_client.query.return_value = {"Items": [{"PK": {"S": "ns123/BUCKET#vip-1#gpt-4#0"}}]}

        event = {
            "action": "apply",
            "table_name": "test-table",
            "namespace_id": "ns123",
            "manifest": {
                "namespace": "test-ns",
                "resources": {"gpt-4": {"disabled": True, "limits": {"rpm": {"capacity": 1000}}}},
                "entities": {
                    "vip-1": {
                        "resources": {
                            "gpt-4": {
                                "disabled": False,
                                "limits": {"rpm": {"capacity": 1000}},
                            }
                        }
                    }
                },
            },
        }
        on_event(event, MagicMock())

        # Two stamp calls: resource-level SET first, entity-level REMOVE last.
        assert mock_client.update_item.call_count == 2
        first_expr = mock_client.update_item.call_args_list[0].kwargs["UpdateExpression"]
        second_expr = mock_client.update_item.call_args_list[1].kwargs["UpdateExpression"]
        assert first_expr == "SET #disabled = :true"
        assert second_expr == "REMOVE #disabled"

    def test_entity_wide_default_directive_fans_out_unscoped_across_resources(
        self, mock_handler_boto3, mock_applier_boto3, mock_urlopen
    ):
        """An entity-wide (`_default_`) directive must fan out unscoped, across
        every real resource the entity has a bucket for -- not scoped to the
        literal `_default_` sentinel.

        Regression test for the round-3 fan-out bug: the pre-fix handler
        passed `resource="_default_"` straight through to `fanout_entity`,
        which queries GSI3 with `sk_prefix="BUCKET#_default_#"`. No real
        bucket is ever keyed by the literal string `_default_` (buckets are
        always keyed by their actual resource name, e.g. `gpt-4`), so that
        query silently matched zero items: the config was written correctly,
        the apply reported success, but no existing bucket was ever stamped.
        The fix translates `_default_` -> `resource=None` (unscoped) before
        calling `fanout_entity`, which queries with `sk_prefix="BUCKET#"` and
        discovers every bucket for the entity across all resources.
        """
        mock_client = self._setup_client(mock_handler_boto3, mock_applier_boto3)
        mock_client.get_item.side_effect = _entity_default_get_item("ns123", "vip-1", True)
        mock_client.query.return_value = {
            "Items": [
                {"PK": {"S": "ns123/BUCKET#vip-1#gpt-4#0"}},
                {"PK": {"S": "ns123/BUCKET#vip-1#claude-3#0"}},
            ]
        }

        event = {
            "action": "apply",
            "table_name": "test-table",
            "namespace_id": "ns123",
            "manifest": {
                "namespace": "test-ns",
                "entities": {
                    "vip-1": {
                        "resources": {
                            "_default_": {
                                "disabled": True,
                                "limits": {"rpm": {"capacity": 1000}},
                            }
                        }
                    }
                },
            },
        }
        result = on_event(event, MagicMock())
        assert result["status"] == "applied"

        # The GSI3 discovery query must be unscoped ("BUCKET#"), never
        # "BUCKET#_default_#" -- this is the exact assertion that
        # distinguishes the fix from the pre-fix bug.
        query_kwargs = mock_client.query.call_args.kwargs
        assert query_kwargs["IndexName"] == "GSI3"
        assert query_kwargs["ExpressionAttributeValues"][":sk"] == {"S": "BUCKET#"}

        # Both buckets, across two different real resources, get stamped.
        stamped_pks = {c.kwargs["Key"]["PK"]["S"] for c in mock_client.update_item.call_args_list}
        assert stamped_pks == {
            "ns123/BUCKET#vip-1#gpt-4#0",
            "ns123/BUCKET#vip-1#claude-3#0",
        }

    def test_cfn_create_with_disabled_resource_fans_out(
        self, mock_handler_boto3, mock_applier_boto3, mock_urlopen
    ):
        """CFN Create with a disabled resource also triggers fan-out.

        `_cfn_properties_to_manifest` now carries the `Disabled` CFN property
        through to the manifest's `disabled` key, so this end-to-end path must
        fan out exactly like the CLI/YAML path does.
        """
        mock_client = self._setup_client(mock_handler_boto3, mock_applier_boto3)
        mock_client.query.return_value = {"Items": [{"PK": {"S": "ns123/BUCKET#user-1#gpt-4#0"}}]}

        event = {
            "RequestType": "Create",
            "ResourceProperties": {
                "ServiceToken": "arn:aws:lambda:us-east-1:123:function:test",
                "TableName": "test-table",
                "Namespace": "test-ns",
                "NamespaceId": "ns123",
                "Resources": {"gpt-4": {"Disabled": True, "Limits": {"rpm": {"Capacity": 1000}}}},
            },
            "ResponseURL": "https://cfn-response.example.com",
            "StackId": "arn:aws:cloudformation:us-east-1:123:stack/test/guid",
            "RequestId": "test-request-id",
            "LogicalResourceId": "TenantLimits",
        }
        result = on_event(event, MagicMock())
        assert result["status"] == "applied"

        # Two passes over the discovery query (Finding 2); the single discovered
        # PK is stamped once thanks to cross-pass de-duplication.
        assert mock_client.query.call_count == 2
        query_kwargs = mock_client.query.call_args.kwargs
        assert query_kwargs["IndexName"] == "GSI2"
        assert query_kwargs["ExpressionAttributeValues"][":pk"] == {"S": "ns123/RESOURCE#gpt-4"}
        mock_client.update_item.assert_called_once()
        update_expr = mock_client.update_item.call_args.kwargs["UpdateExpression"]
        assert update_expr == "SET #disabled = :true"

    def test_cfn_create_with_disabled_absent_key_still_fans_out_as_not_disabled(
        self, mock_handler_boto3, mock_applier_boto3, mock_urlopen
    ):
        """CFN Create with no `Disabled` property still fans out (as `disabled=False`).

        Mirrors `test_apply_without_disabled_key_still_fans_out_as_not_disabled`
        for the CFN entry point: fan-out is unconditional on create/update, so
        the discovery query always runs; with no buckets discovered it is a
        harmless no-op (no `update_item` call).
        """
        mock_client = self._setup_client(mock_handler_boto3, mock_applier_boto3)

        event = {
            "RequestType": "Create",
            "ResourceProperties": {
                "ServiceToken": "arn:aws:lambda:us-east-1:123:function:test",
                "TableName": "test-table",
                "Namespace": "test-ns",
                "NamespaceId": "ns123",
                "Resources": {"gpt-4": {"Limits": {"rpm": {"Capacity": 1000}}}},
            },
            "ResponseURL": "https://cfn-response.example.com",
            "StackId": "arn:aws:cloudformation:us-east-1:123:stack/test/guid",
            "RequestId": "test-request-id",
            "LogicalResourceId": "TenantLimits",
        }
        result = on_event(event, MagicMock())
        assert result["status"] == "applied"
        mock_client.query.assert_called()
        mock_client.update_item.assert_not_called()

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
