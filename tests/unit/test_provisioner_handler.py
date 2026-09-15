"""Tests for the provisioner Lambda handler."""

from unittest.mock import MagicMock, patch

from tests.fixtures.cfn_payloads import (
    RECORDED_CFN_RESOURCE_PROPERTIES,
    RECORDED_CFN_RESOURCE_PROPERTIES_VALID,
)
from zae_limiter.schema import DEFAULT_RESOURCE, pk_entity, pk_resource, sk_config
from zae_limiter_provisioner.differ import Change
from zae_limiter_provisioner.handler import (
    _cfn_limits_to_manifest,
    _cfn_properties_to_manifest,
    _sync_bucket_param_changes,
    on_event,
)


def _disable_stamps(mock_client):
    """The ADR-125 disable/enable stamps among a mock client's update_item calls.

    An apply also writes bucket limit params (#481) through the same client, so
    tests that pin stamping behaviour filter to the `#disabled` writes.
    """
    return [
        c
        for c in mock_client.update_item.call_args_list
        if "#disabled" in c.kwargs.get("UpdateExpression", "")
    ]


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


def _setup_client(
    mock_handler_boto3, mock_applier_boto3, get_item_return=None, disabled_items=None
):
    """Set up shared mock client for both handler and applier boto3.

    `disabled_items` maps (PK, SK) -> bool and stands in for the config
    items `apply_changes` has already written when the fan-out resolves
    them back: both `fanout_resource` and `fanout_entity` re-resolve per
    bucket (ADR-125), so a test that expects stamping has to supply the
    level that decides `disabled`. Any other key falls back to
    `get_item_return`, which is what `_read_provisioner_state` reads.

    Module-level rather than a shared base-class method, because
    `mock.patch` used as a class decorator appends its patchings to the
    *function objects* it finds via `dir()` — inherited ones included — so a
    second test class carrying its own class-level `@patch` stack would
    silently re-decorate the first class's methods and break them all.
    """
    mock_client = MagicMock()
    default_get_item = get_item_return or {}
    if disabled_items:

        def _get_item(*_args, **kwargs):
            raw_key = kwargs["Key"]
            key = (raw_key["PK"]["S"], raw_key["SK"]["S"])
            if key in disabled_items:
                return {"Item": {"disabled": {"BOOL": disabled_items[key]}}}
            return default_get_item

        mock_client.get_item.side_effect = _get_item
    else:
        mock_client.get_item.return_value = default_get_item
    # Default to "no buckets discovered" so fan-out (unconditional on every
    # create/update) doesn't hang: an unconfigured MagicMock response is
    # truthy for `LastEvaluatedKey`, which would loop forever.
    mock_client.query.return_value = {"Items": []}
    mock_handler_boto3.client.return_value = mock_client
    mock_applier_boto3.client.return_value = mock_client
    return mock_client


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

    def _setup_client(
        self, mock_handler_boto3, mock_applier_boto3, get_item_return=None, disabled_items=None
    ):
        return _setup_client(
            mock_handler_boto3, mock_applier_boto3, get_item_return, disabled_items
        )

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
        # fanout_resource re-resolves the discovered bucket's entity (C3, ADR-125);
        # user-1 has no override, so it falls through to the resource-level config
        # apply_changes has just written, which must agree with the True directive.
        mock_client = self._setup_client(
            mock_handler_boto3,
            mock_applier_boto3,
            disabled_items={(pk_resource("ns123", "gpt-4"), sk_config()): True},
        )
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

        # fanout_resource re-resolves the discovered bucket's entity (C3, ADR-125);
        # user-1 has no override, so it falls through to the resource-level config
        # apply_changes has just written. Model that config item's state explicitly
        # (as a real DynamoDB item would look right after each PutItem) and mutate
        # it between the two applies below, rather than letting a stale mock decide.
        resource_key = (pk_resource("ns123", "gpt-4"), sk_config())
        resource_state: dict[str, bool | None] = {"disabled": None}

        def _get_item(*_args, **kwargs):
            raw_key = kwargs["Key"]
            key = (raw_key["PK"]["S"], raw_key["SK"]["S"])
            if key == resource_key:
                if resource_state["disabled"] is None:
                    return {}
                return {"Item": {"disabled": {"BOOL": resource_state["disabled"]}}}
            return {}

        mock_client.get_item.side_effect = _get_item

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
        # apply_changes (mocked put_item) doesn't actually persist anything a
        # later get_item would see, so mirror what it just wrote by hand: the
        # resource config item now has an explicit `disabled: true`.
        resource_state["disabled"] = True
        on_event(disable_event, MagicMock())
        mock_client.update_item.assert_called_once()
        assert (
            mock_client.update_item.call_args.kwargs["UpdateExpression"] == "SET #disabled = :true"
        )

        mock_client.update_item.reset_mock()

        # Second apply: the operator deletes the `disabled: true` line and
        # re-applies. The manifest dict now has no `disabled` key at all, so
        # the PutItem apply_changes issues is a full replace that drops the
        # attribute -- mirror that: the config item still exists but no
        # longer sets `disabled` explicitly.
        resource_state["disabled"] = None
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
        """Resource-level fan-out runs before entity-level, so a carve-out wins (ADR-125).

        fanout_resource now re-resolves each discovered bucket's entity (C3):
        given both user-1 (no override) and vip-1 (explicit `disabled: false`)
        on gpt-4, the resource-level pass must stamp only user-1 and defer
        vip-1 to the entity-level pass that follows -- it must not blindly
        stamp vip-1 and rely on the entity-level pass to undo it, which was
        the pre-C3 (wasteful, and briefly-inconsistent) behavior this test
        used to pin. GSI2 (resource-level discovery) returns both buckets;
        GSI3 (entity-level discovery) returns only vip-1's, exactly as
        production's differently-indexed queries would.
        """
        mock_client = self._setup_client(
            mock_handler_boto3,
            mock_applier_boto3,
            disabled_items={
                (pk_resource("ns123", "gpt-4"), sk_config()): True,
                (pk_entity("ns123", "vip-1"), sk_config("gpt-4")): False,
            },
        )

        def _query(*_args, **kwargs):
            if kwargs["IndexName"] == "GSI2":
                return {
                    "Items": [
                        {"PK": {"S": "ns123/BUCKET#user-1#gpt-4#0"}},
                        {"PK": {"S": "ns123/BUCKET#vip-1#gpt-4#0"}},
                    ]
                }
            return {"Items": [{"PK": {"S": "ns123/BUCKET#vip-1#gpt-4#0"}}]}

        mock_client.query.side_effect = _query

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

        # Two stamp calls: resource-level SET on user-1 (no override) first,
        # entity-level REMOVE on vip-1 (its own carve-out) last. vip-1 is
        # never SET by the resource-level pass -- that would be the wasted,
        # briefly-wrong write the pre-C3 behavior produced.
        #
        # Filtered to the disable stamps: the same apply also writes bucket
        # limit params now (#481), which is a separate concern with its own
        # tests in TestSyncBucketParamChanges.
        stamps = _disable_stamps(mock_client)
        assert len(stamps) == 2
        first_call, second_call = stamps
        assert first_call.kwargs["Key"]["PK"] == {"S": "ns123/BUCKET#user-1#gpt-4#0"}
        assert first_call.kwargs["UpdateExpression"] == "SET #disabled = :true"
        assert second_call.kwargs["Key"]["PK"] == {"S": "ns123/BUCKET#vip-1#gpt-4#0"}
        assert second_call.kwargs["UpdateExpression"] == "REMOVE #disabled"

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
        # distinguishes the fix from the pre-fix bug. The disable fan-out runs
        # first, so its query is the apply's first query (bucket param sync
        # queries afterwards, #481).
        query_kwargs = mock_client.query.call_args_list[0].kwargs
        assert query_kwargs["IndexName"] == "GSI3"
        assert query_kwargs["ExpressionAttributeValues"][":sk"] == {"S": "BUCKET#"}

        # Both buckets, across two different real resources, get stamped.
        stamped_pks = {c.kwargs["Key"]["PK"]["S"] for c in _disable_stamps(mock_client)}
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
        # fanout_resource re-resolves the discovered bucket's entity (C3, ADR-125);
        # user-1 has no override, so it falls through to the resource-level config
        # apply_changes has just written, which must agree with the True directive.
        mock_client = self._setup_client(
            mock_handler_boto3,
            mock_applier_boto3,
            disabled_items={(pk_resource("ns123", "gpt-4"), sk_config()): True},
        )
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


@patch("zae_limiter_provisioner.handler.boto3")
class TestSyncBucketParamChanges:
    """A manifest apply must reach bucket items that already exist (#481).

    `_sync_bucket_param_changes` calls `boto3.client("dynamodb")` itself, so
    `handler.boto3` is patched at class level exactly as `TestProvisionerHandler`
    does for the same reason. Without it a real client is constructed and the
    tests fail with `NoRegionError` on any machine without AWS configured — CI
    included — while passing locally. The client is only threaded through to
    `sync_bucket_params` / `resolve_effective_limits`, which these tests patch,
    so the mock is never exercised beyond construction.
    """

    def test_entity_set_syncs_with_ttl_removed(self, mock_handler_boto3):
        """Entity custom limits mean the bucket must persist: multiplier 0."""
        changes = [
            Change(
                action="update",
                level="entity",
                target="user-1/gpt-4",
                data={"limits": {"rpm": {"capacity": 5, "refill_amount": 5, "refill_period": 60}}},
            )
        ]
        with patch("zae_limiter_provisioner.handler.sync_bucket_params") as sync:
            _sync_bucket_param_changes("tbl", "ns123", changes)
        assert sync.call_count == 1
        kwargs = sync.call_args.kwargs
        assert (kwargs["entity_id"], kwargs["resource"]) == ("user-1", "gpt-4")
        assert kwargs["ttl_multiplier"] == 0
        assert kwargs["stale_limit_names"] is None

    def test_entity_delete_reconciles_to_defaults_with_ttl(self, mock_handler_boto3):
        changes = [
            Change(
                action="delete",
                level="entity",
                target="user-1/gpt-4",
                data={"limits": {"rpm": {"capacity": 5, "refill_amount": 5, "refill_period": 60}}},
            )
        ]
        with (
            patch("zae_limiter_provisioner.handler.sync_bucket_params") as sync,
            patch(
                "zae_limiter_provisioner.handler.resolve_effective_limits",
                return_value={"rpm": {"capacity": 1, "refill_amount": 1, "refill_period": 60}},
            ),
        ):
            _sync_bucket_param_changes("tbl", "ns123", changes)
        kwargs = sync.call_args.kwargs
        assert kwargs["ttl_multiplier"] == 7
        assert kwargs["limits"] == {"rpm": {"capacity": 1, "refill_amount": 1, "refill_period": 60}}

    def test_delete_strips_limits_absent_from_the_new_effective_config(self, mock_handler_boto3):
        changes = [
            Change(
                action="delete",
                level="entity",
                target="user-1/gpt-4",
                data={
                    "limits": {
                        "rpm": {"capacity": 5, "refill_amount": 5, "refill_period": 60},
                        "tpm": {"capacity": 9, "refill_amount": 9, "refill_period": 60},
                    }
                },
            )
        ]
        with (
            patch("zae_limiter_provisioner.handler.sync_bucket_params") as sync,
            patch(
                "zae_limiter_provisioner.handler.resolve_effective_limits",
                return_value={"rpm": {"capacity": 1, "refill_amount": 1, "refill_period": 60}},
            ),
        ):
            _sync_bucket_param_changes("tbl", "ns123", changes)
        assert sync.call_args.kwargs["stale_limit_names"] == {"tpm"}

    def test_entity_set_with_no_declared_limits_is_a_noop(self, mock_handler_boto3):
        """A `disabled`-only entity entry declares no limits: nothing to push.

        `EntityResourceDecl.to_dict()` always emits a `limits` key, empty when
        the manifest entry only carries `disabled`. Syncing that would build a
        `SET` expression with no assignments.
        """
        changes = [
            Change(
                action="update",
                level="entity",
                target="user-1/gpt-4",
                data={"limits": {}, "disabled": True},
            )
        ]
        with patch("zae_limiter_provisioner.handler.sync_bucket_params") as sync:
            _sync_bucket_param_changes("tbl", "ns123", changes)
        sync.assert_not_called()

    def test_resource_and_system_levels_are_never_synced(self, mock_handler_boto3):
        """Buckets on defaults carry a TTL and are recreated (#271, #296)."""
        changes = [
            Change(action="update", level="resource", target="gpt-4", data={"limits": {}}),
            Change(action="update", level="system", target=None, data={"limits": {}}),
        ]
        with patch("zae_limiter_provisioner.handler.sync_bucket_params") as sync:
            _sync_bucket_param_changes("tbl", "ns123", changes)
        sync.assert_not_called()

    def test_entity_id_containing_a_slash_splits_once(self, mock_handler_boto3):
        changes = [
            Change(
                action="update",
                level="entity",
                target="org/team/gpt-4",
                data={"limits": {"rpm": {"capacity": 5, "refill_amount": 5, "refill_period": 60}}},
            )
        ]
        with patch("zae_limiter_provisioner.handler.sync_bucket_params") as sync:
            _sync_bucket_param_changes("tbl", "ns123", changes)
        kwargs = sync.call_args.kwargs
        assert (kwargs["entity_id"], kwargs["resource"]) == ("org", "team/gpt-4")

    def test_delete_with_no_effective_limits_is_a_noop(self, mock_handler_boto3):
        """Nothing left to reconcile to; leave the bucket for its TTL/recreate."""
        changes = [
            Change(action="delete", level="entity", target="user-1/gpt-4", data={"limits": {}})
        ]
        with (
            patch("zae_limiter_provisioner.handler.sync_bucket_params") as sync,
            patch("zae_limiter_provisioner.handler.resolve_effective_limits", return_value={}),
        ):
            _sync_bucket_param_changes("tbl", "ns123", changes)
        sync.assert_not_called()


class TestCfnPropertiesToManifestSchedules:
    """CFN `Schedule` / `ResetSchedule` -> manifest `schedule` / `reset_schedule`.

    The manifest parser validates entries against a strict six-key snake_case
    allowlist, so this direction must produce exactly those keys: a seventh
    key, or a wrong case, is a ValueError inside the Lambda at deploy time.
    """

    def test_schedule_survives_the_return_trip(self):
        from zae_limiter_provisioner.handler import _cfn_limits_to_manifest

        cfn = {
            "rpm": {
                "Capacity": 1000,
                "Schedule": [
                    {"Cron": "* 9-17 * * MON-FRI", "Tz": "America/New_York", "Scale": 0.5},
                    {"Cron": "* 0-6 * * *", "Tz": "UTC", "Capacity": 2000},
                ],
            }
        }
        assert _cfn_limits_to_manifest(cfn)["rpm"]["schedule"] == [
            {"cron": "* 9-17 * * MON-FRI", "tz": "America/New_York", "scale": 0.5},
            {"cron": "* 0-6 * * *", "tz": "UTC", "capacity": 2000},
        ]

    def test_reset_schedule_survives_the_return_trip(self):
        from zae_limiter_provisioner.handler import _cfn_limits_to_manifest

        cfn = {"rpd": {"Capacity": 10000, "ResetSchedule": [{"Cron": "0 0 * * *", "Tz": "UTC"}]}}
        assert _cfn_limits_to_manifest(cfn)["rpd"]["reset_schedule"] == [
            {"cron": "0 0 * * *", "tz": "UTC"}
        ]

    def test_both_tuples_convert_independently(self):
        from zae_limiter_provisioner.handler import _cfn_limits_to_manifest

        result = _cfn_limits_to_manifest(
            {
                "rpd": {
                    "Capacity": 10000,
                    "RefillPeriod": 86400,
                    "Schedule": [{"Cron": "* * * * SAT,SUN", "Scale": 0.5}],
                    "ResetSchedule": [{"Cron": "0 0 * * *", "Tz": "UTC"}],
                }
            }
        )["rpd"]
        assert result["schedule"] == [{"cron": "* * * * SAT,SUN", "scale": 0.5}]
        assert result["reset_schedule"] == [{"cron": "0 0 * * *", "tz": "UTC"}]

    def test_every_entry_field_converts_back(self):
        from zae_limiter_provisioner.handler import _cfn_limits_to_manifest

        cfn = {
            "rpm": {
                "Capacity": 1,
                "Schedule": [
                    {
                        "Cron": "* * * * *",
                        "Tz": "Europe/Paris",
                        "Capacity": 5,
                        "RefillAmount": 6,
                        "RefillPeriodSeconds": 7,
                    }
                ],
            }
        }
        assert _cfn_limits_to_manifest(cfn)["rpm"]["schedule"] == [
            {
                "cron": "* * * * *",
                "tz": "Europe/Paris",
                "capacity": 5,
                "refill_amount": 6,
                "refill_period_seconds": 7,
            }
        ]

    def test_absent_properties_stay_absent(self):
        from zae_limiter_provisioner.handler import _cfn_limits_to_manifest

        result = _cfn_limits_to_manifest({"rpm": {"Capacity": 1000}})["rpm"]
        assert result == {"capacity": 1000}

    def test_empty_and_null_lists_do_not_invent_a_key(self):
        """A key whose value is an empty tuple is omitted by
        ``LimitDecl.to_dict()``; inventing `schedule: []` here would make the
        CFN path's manifest differ from the CLI path's for the same intent."""
        from zae_limiter_provisioner.handler import _cfn_limits_to_manifest

        result = _cfn_limits_to_manifest(
            {"rpm": {"Capacity": 1, "Schedule": [], "ResetSchedule": None}}
        )["rpm"]
        assert result == {"capacity": 1}

    def test_unknown_cfn_entry_keys_are_dropped_not_lowercased(self):
        """A table-driven conversion drops what it does not know. A naive
        "lowercase every key" would forward `Bogus` as `bogus` and the strict
        manifest allowlist would reject the whole apply."""
        from zae_limiter_provisioner.handler import _cfn_limits_to_manifest

        result = _cfn_limits_to_manifest(
            {"rpm": {"Capacity": 1, "Schedule": [{"Cron": "* * * * *", "Scale": 2, "Bogus": 9}]}}
        )["rpm"]
        assert result["schedule"] == [{"cron": "* * * * *", "scale": 2}]

    def test_snake_case_keys_are_not_accepted_as_cfn_input(self):
        """CFN properties are PascalCase. Accepting snake_case here would mask
        a generator that forgot to convert."""
        from zae_limiter_provisioner.handler import _cfn_limits_to_manifest

        result = _cfn_limits_to_manifest(
            {"rpm": {"Capacity": 1, "Schedule": [{"cron": "* * * * *", "scale": 2}]}}
        )["rpm"]
        assert result["schedule"] == [{}]

    def test_schedules_convert_at_system_resource_and_entity_levels(self):
        """`_cfn_limits_to_manifest` is reached from three branches of
        ``_cfn_properties_to_manifest``; and the result must actually parse."""
        from zae_limiter_provisioner.manifest import LimitsManifest

        manifest_data = _cfn_properties_to_manifest(
            {
                "Namespace": "test-ns",
                "System": {
                    "Limits": {
                        "rpm": {"Capacity": 1, "Schedule": [{"Cron": "* * * * *", "Scale": 2.0}]}
                    }
                },
                "Resources": {
                    "gpt-4": {
                        "Limits": {
                            "rpd": {
                                "Capacity": 10,
                                "RefillPeriod": 86400,
                                "ResetSchedule": [{"Cron": "0 0 * * *", "Tz": "UTC"}],
                            }
                        }
                    }
                },
                "Entities": {
                    "vip-1": {
                        "Resources": {
                            "gpt-4": {
                                "Limits": {
                                    "rpm": {
                                        "Capacity": 3,
                                        "Schedule": [{"Cron": "2 * * * *", "Scale": 4.0}],
                                    }
                                }
                            }
                        }
                    }
                },
            }
        )
        assert manifest_data["system"]["limits"]["rpm"]["schedule"] == [
            {"cron": "* * * * *", "scale": 2.0}
        ]
        assert manifest_data["resources"]["gpt-4"]["limits"]["rpd"]["reset_schedule"] == [
            {"cron": "0 0 * * *", "tz": "UTC"}
        ]
        assert manifest_data["entities"]["vip-1"]["resources"]["gpt-4"]["limits"]["rpm"][
            "schedule"
        ] == [{"cron": "2 * * * *", "scale": 4.0}]

        parsed = LimitsManifest.from_dict(manifest_data)
        assert parsed.resources["gpt-4"].limits["rpd"].reset_schedule[0].cron == "0 0 * * *"
        assert parsed.resources["gpt-4"].limits["rpd"].refill_amount == 0

    def test_malformed_shape_reaches_the_manifest_parser(self):
        """A `Schedule` that is not a list of mappings is passed through, not
        swallowed: `_parse_entries` names the offending entry and fails the
        custom resource. Dropping it here would apply the limit with its
        schedule silently missing — the one failure nothing downstream could
        detect."""
        import pytest

        from zae_limiter_provisioner.handler import _cfn_limits_to_manifest
        from zae_limiter_provisioner.manifest import LimitsManifest

        bad_type = _cfn_limits_to_manifest({"rpm": {"Capacity": 1, "Schedule": "0 0 * * *"}})
        assert bad_type["rpm"]["schedule"] == "0 0 * * *"
        with pytest.raises(ValueError, match="schedule must be a list"):
            LimitsManifest.from_dict(
                {"namespace": "n", "resources": {"gpt-4": {"limits": bad_type}}}
            )

        bad_entry = _cfn_limits_to_manifest({"rpm": {"Capacity": 1, "Schedule": ["nope"]}})
        assert bad_entry["rpm"]["schedule"] == ["nope"]
        with pytest.raises(ValueError, match=r"schedule\[0\] must be a mapping"):
            LimitsManifest.from_dict(
                {"namespace": "n", "resources": {"gpt-4": {"limits": bad_entry}}}
            )

    def test_key_tables_are_exact_inverses(self):
        """The generator and the consumer keep independent tables; a silent
        divergence is the one failure mode the round trip cannot self-report."""
        from zae_limiter.limits_cli import _SCHEDULE_KEYS
        from zae_limiter_provisioner.handler import _CFN_SCHEDULE_KEYS

        assert _CFN_SCHEDULE_KEYS == {pascal: snake for snake, pascal in _SCHEDULE_KEYS}


class TestCfnScalarCoercion:
    """CloudFormation stringifies every scalar in `ResourceProperties` (#554).

    Measured against a real `aws cloudformation deploy`: `Disabled: false`
    arrives as `'false'`, `Capacity: 1000` as `'1000'`, `Scale: 0.5` as
    `'0.5'`. Untreated that broke two ways, and the dangerous one was silent:

    | Entry shape | Outcome before the fix |
    |---|---|
    | `Disabled: false` **with** numerics | `TypeError`, apply aborts |
    | `Disabled: false` with **no** numerics | `bool('false')` -> True, **silently disables** |
    | `Schedule` with stringified `Scale` | `TypeError` (`_parse_entries` catches `ValueError`) |

    The silent row is exactly what an ADR-125 carve-out looks like: an entry
    that grants access and declares no limits of its own. Both rows are pinned
    below, and every assertion runs against the recorded payload rather than a
    synthetic dict built from native Python types — the latter is what let this
    ship.
    """

    def test_recorded_payload_carve_outs_survive_as_real_false(self):
        """The headline bug: `Disabled: false` must not become `True`.

        `is False` rather than `== False`, because `'false' == False` is
        already False in Python — the assertion has to pin the *type* too.
        """
        manifest = _cfn_properties_to_manifest(RECORDED_CFN_RESOURCE_PROPERTIES)

        assert manifest["resources"]["gpt-4"]["disabled"] is False
        assert manifest["resources"]["quoted-model"]["disabled"] is False
        assert manifest["resources"]["enabled-model"]["disabled"] is True
        assert manifest["entities"]["user-premium"]["resources"]["gpt-4"]["disabled"] is False

    def test_recorded_payload_silent_shape_survives_manifest_parsing(self):
        """The silent case, end to end through `LimitsManifest`.

        `quoted-model` carries `Disabled` and **no** limits — nothing numeric to
        trip a `TypeError`, so before the fix this parsed cleanly and disabled a
        resource the operator was re-enabling.
        """
        from zae_limiter_provisioner.manifest import LimitsManifest

        parsed = LimitsManifest.from_dict(
            _cfn_properties_to_manifest(
                {"Namespace": "n", "Resources": {"quoted-model": {"Disabled": "false"}}}
            )
        )
        assert parsed.resources["quoted-model"].disabled is False

    def test_recorded_payload_numeric_fields_become_numbers(self):
        """The loud case: stringified numerics reach `LimitDecl` as numbers."""
        manifest = _cfn_properties_to_manifest(RECORDED_CFN_RESOURCE_PROPERTIES)

        system_rpm = manifest["system"]["limits"]["rpm"]
        assert system_rpm["capacity"] == 1000
        assert isinstance(system_rpm["capacity"], int)
        assert system_rpm["refill_amount"] == 1000
        assert isinstance(system_rpm["refill_amount"], int)

        entry = manifest["resources"]["gpt-4"]["limits"]["rpm"]["schedule"][0]
        assert entry["scale"] == 0.5
        assert isinstance(entry["scale"], float)
        assert entry["capacity"] == 2000
        assert isinstance(entry["capacity"], int)

    def test_recorded_payload_cron_and_tz_stay_strings(self):
        """Type-directed, not value-sniffing.

        `Cron` is legitimately a string whose content is entirely digits and
        punctuation. A generic "looks numeric => int" pass — the shape of the
        obvious wrong fix — would mangle it.
        """
        manifest = _cfn_properties_to_manifest(RECORDED_CFN_RESOURCE_PROPERTIES)
        entries = manifest["resources"]["gpt-4"]["limits"]["rpm"]["schedule"]
        assert entries[0]["cron"] == "0 9 * * 1-5"
        assert entries[0]["tz"] == "America/New_York"
        assert entries[1]["cron"] == "0 18 * * 1-5"

        numeric_looking = _cfn_limits_to_manifest(
            {"rpm": {"Capacity": "1", "Schedule": [{"Cron": "5 4 3 2 1", "Scale": "2"}]}}
        )
        cron = numeric_looking["rpm"]["schedule"][0]["cron"]
        assert cron == "5 4 3 2 1"
        assert isinstance(cron, str)
        assert isinstance(entries[0]["tz"], str)

    def test_recorded_payload_residual_failure_is_a_named_value_error(self):
        """The recorded payload's own quirk, pinned so it is not mistaken for #554.

        Its first schedule entry sets both `Scale` and `Capacity`, which
        `ScheduleEntry` forbids. After coercion that surfaces as a `ValueError`
        naming the entry — reportable back through the CFN response — where
        before it was a `TypeError` from comparing `str` to `int`.
        """
        import pytest

        from zae_limiter_provisioner.manifest import LimitsManifest

        manifest = _cfn_properties_to_manifest(RECORDED_CFN_RESOURCE_PROPERTIES)
        with pytest.raises(ValueError, match=r"schedule\[0\]: a schedule entry must set exactly"):
            LimitsManifest.from_dict(manifest)

    def test_corrected_recorded_payload_parses_end_to_end(self):
        """Same payload, one illegal entry fixed: every scalar still a string."""
        from zae_limiter.schedule import ScheduleEntry
        from zae_limiter_provisioner.manifest import LimitsManifest

        parsed = LimitsManifest.from_dict(
            _cfn_properties_to_manifest(RECORDED_CFN_RESOURCE_PROPERTIES_VALID)
        )
        assert parsed.system is not None
        assert parsed.system.limits["rpm"].capacity == 1000
        assert parsed.resources["gpt-4"].disabled is False
        assert parsed.resources["gpt-4"].limits["rpm"].capacity == 500
        assert parsed.resources["gpt-4"].limits["rpm"].schedule == (
            ScheduleEntry(cron="0 9 * * 1-5", tz="America/New_York", scale=0.5),
            ScheduleEntry(cron="0 18 * * 1-5", scale=1.0),
        )
        assert parsed.entities["user-premium"].resources["gpt-4"].disabled is False
        assert parsed.entities["user-premium"].resources["gpt-4"].limits["rpm"].capacity == 100

    def test_disabled_accepts_any_case(self):
        """`Disabled: "True"` — quoted, so YAML keeps it a string — is truthy by
        accident under `bool()`. The allowlist lowercases before matching so it
        is truthy on purpose, and `"FALSE"` is not truthy at all."""
        manifest = _cfn_properties_to_manifest(
            {
                "Namespace": "n",
                "Resources": {
                    "a": {"Disabled": "True"},
                    "b": {"Disabled": "FALSE"},
                    "c": {"Disabled": "TrUe"},
                    "d": {"Disabled": "fAlSe"},
                },
            }
        )
        assert manifest["resources"]["a"]["disabled"] is True
        assert manifest["resources"]["b"]["disabled"] is False
        assert manifest["resources"]["c"]["disabled"] is True
        assert manifest["resources"]["d"]["disabled"] is False

    def test_disabled_raises_on_anything_outside_the_allowlist(self):
        """Raise rather than fall through to `bool(v)`.

        `disabled` is a kill switch: guessing wrong either locks a tenant out or
        re-admits one that was meant to stay out, and both are silent. `'1'` and
        `'yes'` are the plausible spellings an operator might reach for; `''` is
        what a CloudFormation `Default: ""` parameter delivers.
        """
        import pytest

        for value in ("yes", "no", "1", "0", "", "none", 1, 0, None, [], 1.0):
            with pytest.raises(ValueError, match="must be true or false"):
                _cfn_properties_to_manifest(
                    {"Namespace": "n", "Resources": {"gpt-4": {"Disabled": value}}}
                )

    def test_absent_disabled_is_still_absent_among_stringified_siblings(self):
        """The tri-state's third state. Coercion converts a present value; it
        must never invent one, or every apply would re-enable whatever the
        operator disabled out of band."""
        manifest = _cfn_properties_to_manifest(
            {
                "Namespace": "n",
                "Resources": {"gpt-4": {"Limits": {"rpm": {"Capacity": "1000"}}}},
                "Entities": {
                    "vip": {"Resources": {"gpt-4": {"Limits": {"rpm": {"Capacity": "10"}}}}}
                },
            }
        )
        assert "disabled" not in manifest["resources"]["gpt-4"]
        assert "disabled" not in manifest["entities"]["vip"]["resources"]["gpt-4"]

    def test_coercion_is_idempotent_on_already_native_values(self):
        """Re-invokes can hand back values a previous pass converted, and if AWS
        ever ships the fix (cloudformation-coverage-roadmap#1037) this boundary
        keeps working instead of breaking on real types."""
        native = {
            "Namespace": "n",
            "Resources": {
                "gpt-4": {
                    "Disabled": False,
                    "Limits": {
                        "rpm": {
                            "Capacity": 500,
                            "RefillAmount": 250,
                            "RefillPeriod": 60,
                            "Schedule": [{"Cron": "0 9 * * *", "Scale": 0.5}],
                        }
                    },
                }
            },
        }
        manifest = _cfn_properties_to_manifest(native)
        limit = manifest["resources"]["gpt-4"]["limits"]["rpm"]
        assert manifest["resources"]["gpt-4"]["disabled"] is False
        assert limit["capacity"] == 500 and isinstance(limit["capacity"], int)
        assert limit["refill_amount"] == 250
        assert limit["refill_period"] == 60
        assert limit["schedule"] == [{"cron": "0 9 * * *", "scale": 0.5}]

        # Idempotent in the strict sense: running the CFN conversion over its own
        # PascalCase input twice changes nothing.
        assert _cfn_properties_to_manifest(native) == manifest

    def test_numeric_properties_reject_unparseable_and_boolean_values(self):
        import pytest

        for value in ("abc", "0.5", "1e3x", None, [], True, False):
            with pytest.raises(ValueError, match="must be a whole number"):
                _cfn_limits_to_manifest({"rpm": {"Capacity": value}})

    def test_scale_rejects_non_finite_values(self):
        """`ScheduleEntry` validates `scale` with `scale <= 0`, and every
        comparison against NaN is False — so a NaN would pass validation and
        then poison every effective-parameter calculation. Rejected here, where
        the field's target type is known."""
        import pytest

        for value in ("NaN", "nan", "Infinity", "inf", "-inf", float("nan")):
            with pytest.raises(ValueError, match="must be a finite number"):
                _cfn_limits_to_manifest(
                    {"rpm": {"Capacity": "1", "Schedule": [{"Cron": "* * * * *", "Scale": value}]}}
                )

        with pytest.raises(ValueError, match="must be a number"):
            _cfn_limits_to_manifest(
                {"rpm": {"Capacity": "1", "Schedule": [{"Cron": "* * * * *", "Scale": "half"}]}}
            )

    def test_empty_string_drops_an_optional_numeric_property(self):
        """A CloudFormation `Parameter: {Default: ""}` is how a template spells
        "not set" for an optional property; the RPDK's own recast maps `""` to
        absent for numeric targets. Dropping the key lets `LimitDecl`'s
        documented default apply."""
        result = _cfn_limits_to_manifest(
            {"rpm": {"Capacity": "500", "RefillAmount": "", "RefillPeriod": ""}}
        )
        assert result == {"rpm": {"capacity": 500}}

        entry = _cfn_limits_to_manifest(
            {
                "rpm": {
                    "Capacity": "1",
                    "Schedule": [{"Cron": "* * * * *", "Scale": "2", "Capacity": ""}],
                }
            }
        )
        assert entry["rpm"]["schedule"] == [{"cron": "* * * * *", "scale": 2.0}]

    def test_empty_capacity_is_an_error_not_a_dropped_key(self):
        """`capacity` is the one field with no default — the allowance itself.
        Dropping it would surface as a `KeyError` naming a snake_case key the
        operator never wrote."""
        import pytest

        with pytest.raises(ValueError, match=r"Limits\.rpm\.Capacity is required"):
            _cfn_limits_to_manifest({"rpm": {"Capacity": ""}})

    def test_errors_name_the_full_dotted_property_path(self):
        """A failing apply is read in CloudWatch, where the only context is the
        message. Each of the four branches must name where it was."""
        import pytest

        cases = [
            ({"System": {"Limits": {"rpm": {"Capacity": "x"}}}}, "System.Limits.rpm.Capacity"),
            (
                {"Resources": {"gpt-4": {"Limits": {"rpm": {"Capacity": "x"}}}}},
                "Resources.gpt-4.Limits.rpm.Capacity",
            ),
            ({"Resources": {"gpt-4": {"Disabled": "maybe"}}}, "Resources.gpt-4.Disabled"),
            (
                {"Entities": {"vip": {"Resources": {"gpt-4": {"Disabled": "maybe"}}}}},
                "Entities.vip.Resources.gpt-4.Disabled",
            ),
            (
                {
                    "Resources": {
                        "gpt-4": {
                            "Limits": {
                                "rpm": {
                                    "Capacity": "1",
                                    "Schedule": [{"Cron": "* * * * *", "Scale": "x"}],
                                }
                            }
                        }
                    }
                },
                "Resources.gpt-4.Limits.rpm.Schedule[0].Scale",
            ),
        ]
        for props, expected in cases:
            with pytest.raises(ValueError) as exc:
                _cfn_properties_to_manifest({"Namespace": "n", **props})
            assert expected in str(exc.value)

    def test_every_schedule_property_declares_a_target_type(self):
        """The coercion is type-directed, so the type table must cover the key
        table exactly. A seventh schedule property added to one and not the
        other would either KeyError at runtime or silently skip coercion."""
        from zae_limiter_provisioner.handler import _CFN_SCHEDULE_COERCERS, _CFN_SCHEDULE_KEYS

        assert set(_CFN_SCHEDULE_COERCERS) == set(_CFN_SCHEDULE_KEYS)


@patch("zae_limiter_provisioner.handler.urllib.request.urlopen")
@patch("zae_limiter_provisioner.applier.boto3")
@patch("zae_limiter_provisioner.handler.boto3")
class TestCfnScalarCoercionEndToEnd:
    """The #554 inversion driven through `on_event`, not just the converter."""

    _setup_client = staticmethod(_setup_client)

    def test_stringified_disabled_false_unstamps_instead_of_stamping(
        self, mock_handler_boto3, mock_applier_boto3, mock_urlopen
    ):
        """A carve-out delivered as `'false'` must clear the stamp, not set it.

        This is the whole bug in one assertion: `bool('false')` is `True`, so
        before the fix this apply wrote `SET #disabled = :true` over the buckets
        of a resource the operator was explicitly re-enabling, reported SUCCESS
        to CloudFormation, and left no trace.
        """
        mock_client = self._setup_client(
            mock_handler_boto3,
            mock_applier_boto3,
            disabled_items={(pk_resource("ns123", "gpt-4"), sk_config()): False},
        )
        mock_client.query.return_value = {"Items": [{"PK": {"S": "ns123/BUCKET#user-1#gpt-4#0"}}]}

        event = {
            "RequestType": "Create",
            "ResourceProperties": {
                "ServiceToken": "arn:aws:lambda:us-east-1:123:function:test",
                "TableName": "test-table",
                "Namespace": "test-ns",
                "NamespaceId": "ns123",
                # Exactly as CloudFormation delivers it: strings, not natives.
                "Resources": {"gpt-4": {"Disabled": "false", "Limits": {}}},
            },
            "ResponseURL": "https://cfn-response.example.com",
            "StackId": "arn:aws:cloudformation:us-east-1:123:stack/test/guid",
            "RequestId": "test-request-id",
            "LogicalResourceId": "TenantLimits",
        }
        result = on_event(event, MagicMock())
        assert result["status"] == "applied"

        stamps = _disable_stamps(mock_client)
        assert [c.kwargs["UpdateExpression"] for c in stamps] == ["REMOVE #disabled"]

    def test_stringified_true_still_stamps(
        self, mock_handler_boto3, mock_applier_boto3, mock_urlopen
    ):
        """The other half of the allowlist — otherwise a constant `False` would
        pass the test above."""
        mock_client = self._setup_client(
            mock_handler_boto3,
            mock_applier_boto3,
            disabled_items={(pk_resource("ns123", "gpt-4"), sk_config()): True},
        )
        mock_client.query.return_value = {"Items": [{"PK": {"S": "ns123/BUCKET#user-1#gpt-4#0"}}]}

        event = {
            "RequestType": "Create",
            "ResourceProperties": {
                "ServiceToken": "arn:aws:lambda:us-east-1:123:function:test",
                "TableName": "test-table",
                "Namespace": "test-ns",
                "NamespaceId": "ns123",
                "Resources": {"gpt-4": {"Disabled": "true", "Limits": {}}},
            },
            "ResponseURL": "https://cfn-response.example.com",
            "StackId": "arn:aws:cloudformation:us-east-1:123:stack/test/guid",
            "RequestId": "test-request-id",
            "LogicalResourceId": "TenantLimits",
        }
        assert on_event(event, MagicMock())["status"] == "applied"
        stamps = _disable_stamps(mock_client)
        assert [c.kwargs["UpdateExpression"] for c in stamps] == ["SET #disabled = :true"]

    def test_stringified_numeric_limits_apply_without_a_type_error(
        self, mock_handler_boto3, mock_applier_boto3, mock_urlopen
    ):
        """The loud half of the blast radius, driven through `on_event`.

        Before the fix `'1000' <= 0` raised `TypeError` inside
        `LimitDecl.from_dict`, the custom resource reported FAILED and nothing
        was written at all.
        """
        self._setup_client(mock_handler_boto3, mock_applier_boto3)

        event = {
            "RequestType": "Create",
            "ResourceProperties": {
                "ServiceToken": "arn:aws:lambda:us-east-1:123:function:test",
                "TableName": "test-table",
                "Namespace": "test-ns",
                "NamespaceId": "ns123",
                "System": {
                    "OnUnavailable": "block",
                    "Limits": {"rpm": {"Capacity": "1000", "RefillPeriod": "60"}},
                },
                "Resources": {
                    "gpt-4": {
                        "Disabled": "false",
                        "Limits": {"rpm": {"Capacity": "500", "RefillAmount": "500"}},
                    }
                },
            },
            "ResponseURL": "https://cfn-response.example.com",
            "StackId": "arn:aws:cloudformation:us-east-1:123:stack/test/guid",
            "RequestId": "test-request-id",
            "LogicalResourceId": "TenantLimits",
        }
        result = on_event(event, MagicMock())
        assert result["status"] == "applied"
        assert ("system", None, "create") in {
            (c["level"], c["target"], c["action"]) for c in result["changes"]
        }

    def test_old_resource_properties_are_ignored_on_update(
        self, mock_handler_boto3, mock_applier_boto3, mock_urlopen
    ):
        """`OldResourceProperties` is present on Update and stringified too, but
        nothing in this package reads it — verified by grep, pinned here.

        If drift/diff logic is ever added it must run the old properties through
        the same coercion; comparing coerced-new against stringified-old would
        report a change in every field on every re-apply. This test fails the
        moment the old properties start influencing the outcome without going
        through `_cfn_properties_to_manifest`.
        """
        self._setup_client(mock_handler_boto3, mock_applier_boto3)

        base = {
            "RequestType": "Update",
            "ResourceProperties": {
                "ServiceToken": "arn:aws:lambda:us-east-1:123:function:test",
                "TableName": "test-table",
                "Namespace": "test-ns",
                "NamespaceId": "ns123",
                "Resources": {"gpt-4": {"Limits": {"rpm": {"Capacity": "2000"}}}},
            },
            "ResponseURL": "https://cfn-response.example.com",
            "StackId": "arn:aws:cloudformation:us-east-1:123:stack/test/guid",
            "RequestId": "test-request-id",
            "LogicalResourceId": "TenantLimits",
        }
        without_old = on_event(dict(base), MagicMock())
        with_old = on_event(
            {
                **base,
                "OldResourceProperties": {
                    "TableName": "test-table",
                    "Namespace": "test-ns",
                    "NamespaceId": "ns123",
                    "Resources": {
                        "claude-3": {"Disabled": "true", "Limits": {"tpm": {"Capacity": "9"}}}
                    },
                },
            },
            MagicMock(),
        )
        assert without_old["changes"] == with_old["changes"]
