"""Tests for the provisioner Lambda handler."""

import json
from unittest.mock import MagicMock, patch

import pytest

from tests.fixtures.cfn_payloads import (
    RECORDED_CFN_RESOURCE_PROPERTIES,
    RECORDED_CFN_RESOURCE_PROPERTIES_VALID,
)
from zae_limiter.exceptions import VersionMismatchError
from zae_limiter.schema import (
    DEFAULT_RESOURCE,
    pk_entity,
    pk_resource,
    pk_system,
    sk_config,
    sk_provisioner,
)
from zae_limiter_provisioner.differ import Change
from zae_limiter_provisioner.fanout import fanout_cascade as real_fanout_cascade
from zae_limiter_provisioner.fanout import fanout_entity as real_fanout_entity
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


def _bucket_queries(mock_client):
    """The bucket discovery queries (GSI2/GSI3/GSI4) a mock client made.

    Every apply also queries the table for pending fan-out items
    (`pending.read_pending`), which these tests do not count.
    """
    return [c for c in mock_client.query.call_args_list if "IndexName" in c.kwargs]


def _bucket_updates(mock_client):
    """The `update_item` calls a mock client made against bucket items."""
    return [
        c for c in mock_client.update_item.call_args_list if "BUCKET#" in c.kwargs["Key"]["PK"]["S"]
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
        """An absent `System.Disabled` is not invented (a present one is rejected, #693)."""
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
        (stamp,) = _bucket_updates(mock_client)
        assert stamp.kwargs["Key"]["PK"] == {"S": "ns123/BUCKET#user-1#gpt-4#0"}

    def test_a_level_whose_disabled_did_not_change_is_not_fanned_out(
        self, mock_handler_boto3, mock_applier_boto3, mock_urlopen
    ):
        """A resource that neither had nor has `disabled` runs no disable fan-out.

        The fan-out is change-only: it runs when the write's `ALL_OLD` image and
        the new item disagree on `disabled` (`ApplyResult.disabled_changed`).
        Absent before and absent after cannot change any bucket's resolution,
        so not even the discovery query runs — that query, and the per-entity
        resolution behind it, was most of an apply's cost on a large manifest.
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

        assert _bucket_queries(mock_client) == []
        assert _bucket_updates(mock_client) == []

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

        # The disable fan-out is change-only, decided off each write's ALL_OLD
        # image, so the PutItem must return what it replaced, as DynamoDB does.
        stored: dict[tuple[str, str], dict] = {}

        def _put_item(*_args, **kwargs):
            item = kwargs["Item"]
            key = (item["PK"]["S"], item["SK"]["S"])
            old = stored.get(key)
            stored[key] = item
            return {"Attributes": old} if old else {}

        mock_client.put_item.side_effect = _put_item

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
        (stamp,) = _bucket_updates(mock_client)
        assert stamp.kwargs["UpdateExpression"] == "SET #disabled = :true REMOVE #byp"

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

        (stamp,) = _bucket_updates(mock_client)
        assert stamp.kwargs["UpdateExpression"] == "REMOVE #disabled, #byp"

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
            if kwargs.get("IndexName") == "GSI2":
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
        assert first_call.kwargs["UpdateExpression"] == "SET #disabled = :true REMOVE #byp"
        assert second_call.kwargs["Key"]["PK"] == {"S": "ns123/BUCKET#vip-1#gpt-4#0"}
        assert second_call.kwargs["UpdateExpression"] == "REMOVE #disabled, #byp"

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
        query_kwargs = _bucket_queries(mock_client)[0].kwargs
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
        assert len(_bucket_queries(mock_client)) == 2
        query_kwargs = mock_client.query.call_args.kwargs
        assert query_kwargs["IndexName"] == "GSI2"
        assert query_kwargs["ExpressionAttributeValues"][":pk"] == {"S": "ns123/RESOURCE#gpt-4"}
        (stamp,) = _bucket_updates(mock_client)
        update_expr = stamp.kwargs["UpdateExpression"]
        assert update_expr == "SET #disabled = :true REMOVE #byp"

    def test_cfn_create_with_disabled_absent_key_is_not_fanned_out(
        self, mock_handler_boto3, mock_applier_boto3, mock_urlopen
    ):
        """CFN Create with no `Disabled` property runs no disable fan-out.

        Mirrors `test_a_level_whose_disabled_did_not_change_is_not_fanned_out`
        for the CFN entry point.
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
        assert _bucket_queries(mock_client) == []
        assert _bucket_updates(mock_client) == []

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

    def test_a_change_with_nothing_to_sync_is_still_reported_done(self, mock_handler_boto3):
        """Its pending item must go: nothing is left to sync for it."""
        done = MagicMock()
        changes = [
            Change(action="update", level="entity", target="u1/gpt-4", data={"limits": {}}),
            Change(action="delete", level="entity", target="u2/gpt-4"),
        ]
        with (
            patch("zae_limiter_provisioner.handler.sync_bucket_params") as sync,
            patch("zae_limiter_provisioner.handler.resolve_effective_limits", return_value={}),
        ):
            _sync_bucket_param_changes("tbl", "ns123", changes, on_done=done)
        sync.assert_not_called()
        assert [c.args for c in done.call_args_list] == [
            ("sync", "entity", "u1/gpt-4"),
            ("sync", "entity", "u2/gpt-4"),
        ]

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

    def test_each_change_is_timed_on_its_own(self, mock_handler_boto3):
        """Review E4: one instant for a whole apply stamped soft deadlines
        already expired on a long one; each change's sync is timed just
        before it runs (ADR-151 decision 8)."""
        limits = {"rpm": {"capacity": 5, "refill_amount": 5, "refill_period": 60}}
        changes = [
            Change(
                action="update", level="entity", target=f"user-{i}/gpt-4", data={"limits": limits}
            )
            for i in (1, 2)
        ]
        with (
            patch("zae_limiter_provisioner.handler.sync_bucket_params") as sync,
            patch("zae_limiter_provisioner.handler.time.time", side_effect=[1.0, 51.0]),
        ):
            _sync_bucket_param_changes("tbl", "ns123", changes)
        assert [c.kwargs["now_ms"] for c in sync.call_args_list] == [1_000, 51_000]


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

    def test_stringified_payload_equals_the_native_typed_equivalent(self):
        """The strongest statement of the fix: after coercion, the two inputs
        are indistinguishable downstream.

        The native side is written out literally rather than derived from the
        recorded payload, so this compares the boundary's output against an
        independently-stated expectation instead of against itself.
        """
        from zae_limiter_provisioner.manifest import LimitsManifest

        native = {
            "ServiceToken": "arn:aws:lambda:us-east-1:733153035800:function:probe",
            "Namespace": "default",
            "System": {
                "OnUnavailable": "block",
                "Limits": {"rpm": {"Capacity": 1000, "RefillAmount": 1000}},
            },
            "Resources": {
                "gpt-4": {
                    "Disabled": False,
                    "Limits": {
                        "rpm": {
                            "Capacity": 500,
                            "Schedule": [
                                {
                                    "Cron": "0 9 * * 1-5",
                                    "Tz": "America/New_York",
                                    "Scale": 0.5,
                                },
                                {"Cron": "0 18 * * 1-5", "Scale": 1},
                            ],
                        }
                    },
                },
                "quoted-model": {"Disabled": False},
                "enabled-model": {"Disabled": True},
            },
            "Entities": {
                "user-premium": {
                    "Resources": {
                        "gpt-4": {"Disabled": False, "Limits": {"rpm": {"Capacity": 100}}}
                    }
                }
            },
        }
        from_strings = LimitsManifest.from_dict(
            _cfn_properties_to_manifest(RECORDED_CFN_RESOURCE_PROPERTIES_VALID)
        )
        from_natives = LimitsManifest.from_dict(_cfn_properties_to_manifest(native))
        assert from_strings == from_natives

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

    def test_scale_rejects_booleans_and_non_scalar_shapes(self):
        """`Scale: true` is a mistake, not the number 1 — and `bool` is an `int`
        subclass, so it would otherwise slip through the numeric branch."""
        import pytest

        with pytest.raises(ValueError, match="got boolean"):
            _cfn_limits_to_manifest(
                {"rpm": {"Capacity": "1", "Schedule": [{"Cron": "* * * * *", "Scale": True}]}}
            )
        for value in (None, [], {"a": 1}):
            with pytest.raises(ValueError, match="must be a number"):
                _cfn_limits_to_manifest(
                    {"rpm": {"Capacity": "1", "Schedule": [{"Cron": "* * * * *", "Scale": value}]}}
                )

    def test_empty_scale_drops_the_property(self):
        """Same `Default: ""` idiom as the integer fields."""
        result = _cfn_limits_to_manifest(
            {
                "rpm": {
                    "Capacity": "1",
                    "Schedule": [{"Cron": "* * * * *", "Scale": "", "Capacity": "5"}],
                }
            }
        )
        assert result["rpm"]["schedule"] == [{"cron": "* * * * *", "capacity": 5}]

    def test_string_properties_reject_non_strings(self):
        """`Cron`/`Tz` have a declared target type too. A non-string can only
        reach here from a direct Lambda invoke, and without this it surfaces as
        an `AttributeError` inside `parse_cron` naming no property at all."""
        import pytest

        with pytest.raises(ValueError, match=r"Schedule\[0\]\.Cron must be a string"):
            _cfn_limits_to_manifest(
                {"rpm": {"Capacity": "1", "Schedule": [{"Cron": 5, "Scale": "2"}]}}
            )
        with pytest.raises(ValueError, match=r"Schedule\[0\]\.Tz must be a string"):
            _cfn_limits_to_manifest(
                {"rpm": {"Capacity": "1", "Schedule": [{"Cron": "* * * * *", "Tz": 5}]}}
            )
        with pytest.raises(ValueError, match="System.OnUnavailable must be a string"):
            _cfn_properties_to_manifest({"Namespace": "n", "System": {"OnUnavailable": 1}})

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


class TestCfnDurationWindow:
    """`ResetAfterSeconds` (ADR-139, plan Task 13) — the third recovery
    spelling's CloudFormation round trip, mirroring the `RefillAmount`/
    `RefillPeriod` coverage above."""

    def test_cfn_round_trip_of_a_duration_window(self):
        """CloudFormation delivers EVERY property as a string (#554), so the
        coercer is not optional here."""
        from zae_limiter_provisioner.handler import _CFN_LIMIT_OPTIONAL_KEYS

        manifest_key, coercer = _CFN_LIMIT_OPTIONAL_KEYS["ResetAfterSeconds"]
        assert manifest_key == "reset_after_seconds"
        assert coercer("18000", "ResetAfterSeconds") == 18_000
        import pytest

        with pytest.raises(ValueError, match="whole number"):
            coercer("5h", "ResetAfterSeconds")

    def test_reset_after_seconds_reaches_the_manifest_shape(self):
        result = _cfn_limits_to_manifest(
            {"session": {"Capacity": "10000", "ResetAfterSeconds": "18000"}}
        )
        assert result["session"]["reset_after_seconds"] == 18_000

    def test_empty_string_drops_reset_after_seconds(self):
        """Same optional-property rule as `RefillAmount`/`RefillPeriod`."""
        result = _cfn_limits_to_manifest({"rpm": {"Capacity": "500", "ResetAfterSeconds": ""}})
        assert result == {"rpm": {"capacity": 500}}

    def test_a_bool_reset_after_seconds_is_rejected(self):
        import pytest

        with pytest.raises(ValueError, match="boolean"):
            _cfn_limits_to_manifest({"session": {"Capacity": "1", "ResetAfterSeconds": True}})

    def test_cli_and_handler_round_trip_reset_after_seconds(self):
        """`limits_cli._limits_to_cfn` (the generator) and
        `handler._cfn_limits_to_manifest` (the reader) must agree on the
        property name — the same role `_SCHEDULE_KEYS`/`_CFN_SCHEDULE_KEYS`
        play for schedule entries, but `_CFN_LIMIT_OPTIONAL_KEYS` has no
        peer table in `limits_cli` (`_limits_to_cfn`'s branch is hand-coded),
        so the round trip itself is the test rather than a second key table."""
        from zae_limiter.limits_cli import _limits_to_cfn

        manifest_limits = {"session": {"capacity": 10_000, "reset_after_seconds": 18_000}}
        cfn = _limits_to_cfn(manifest_limits)
        assert cfn["session"]["ResetAfterSeconds"] == 18_000

        # CloudFormation stringifies every scalar (#554) before the Lambda
        # ever sees it.
        stringified = {"session": {"Capacity": "10000", "ResetAfterSeconds": "18000"}}
        assert _cfn_limits_to_manifest(stringified)["session"]["reset_after_seconds"] == 18_000


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
        assert [c.kwargs["UpdateExpression"] for c in stamps] == ["REMOVE #disabled, #byp"]

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
        assert [c.kwargs["UpdateExpression"] for c in stamps] == [
            "SET #disabled = :true REMOVE #byp"
        ]

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


# ---------------------------------------------------------------------------
# #563 — a post-commit fan-out failure must not lose the #PROVISIONER record
# ---------------------------------------------------------------------------

# A compact schedule string this provisioner cannot decode. The realistic
# source is an item written by a NEWER client carrying an encoding this one
# does not understand — design §4.1 ships no version marker (#515), so a
# forward-compatible read is not available and `decode` raises.
_UNDECODABLE_SCHED = "zz!!garbage"

_CORRUPT_SYSTEM_CONFIG = {
    "Item": {
        "PK": {"S": pk_system("ns123")},
        "SK": {"S": sk_config()},
        "l_rpm_cp": {"N": "1000"},
        "l_rpm_ra": {"N": "1000"},
        "l_rpm_rp": {"N": "60"},
        "l_rpm_sched": {"S": _UNDECODABLE_SCHED},
    }
}

_PREVIOUS_STATE = {
    "Item": {
        "managed_system": {"BOOL": False},
        "managed_resources": {"L": []},
        "managed_entities": {"M": {"user-1": {"L": [{"S": "gpt-4"}]}}},
    }
}


def _corrupt_system_schedule_client(mock_handler_boto3, mock_applier_boto3):
    """A client whose SYSTEM config item carries an undecodable schedule.

    System is chosen deliberately: an apply that drops an entity's config does
    not rewrite the system level, so the corrupt item is still there when
    `_sync_bucket_param_changes` reconciles the deleted entity down to it. Put
    the same string on a level the manifest DOES declare and `apply_changes`
    overwrites it before the sync ever reads it, and the whole scenario passes
    for the wrong reason.
    """
    mock_client = MagicMock()

    def _get_item(*_args, **kwargs):
        key = (kwargs["Key"]["PK"]["S"], kwargs["Key"]["SK"]["S"])
        if key == (pk_system("ns123"), sk_provisioner()):
            return _PREVIOUS_STATE
        if key == (pk_system("ns123"), sk_config()):
            return _CORRUPT_SYSTEM_CONFIG
        return {}

    mock_client.get_item.side_effect = _get_item
    mock_client.delete_item.return_value = _DELETED_ENTITY_CONFIG
    mock_client.query.return_value = {"Items": []}
    mock_handler_boto3.client.return_value = mock_client
    mock_applier_boto3.client.return_value = mock_client
    return mock_client


# The `ALL_OLD` image of a deleted entity config. The bucket param sync is
# change-only, so a delete syncs only when the deleted item held limits.
_DELETED_ENTITY_CONFIG = {
    "Attributes": {
        "l_rpm_cp": {"N": "5"},
        "l_rpm_ra": {"N": "5"},
        "l_rpm_rp": {"N": "60"},
    }
}


def _provisioner_writes(mock_client):
    """The `#PROVISIONER` state records written through a mock client."""
    return [
        c
        for c in mock_client.put_item.call_args_list
        if c.kwargs["Item"]["SK"]["S"] == sk_provisioner()
    ]


@patch("zae_limiter_provisioner.handler.urllib.request.urlopen")
@patch("zae_limiter_provisioner.applier.boto3")
@patch("zae_limiter_provisioner.handler.boto3")
class TestPostCommitFanoutFailure:
    """#563: `apply_changes` commits before the fan-outs run, so a fan-out
    failure must report partial progress — never abandon the state record."""

    def test_cfn_records_state_and_reports_success_with_errors(
        self, mock_handler_boto3, mock_applier_boto3, mock_urlopen
    ):
        mock_client = _corrupt_system_schedule_client(mock_handler_boto3, mock_applier_boto3)

        event = {
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

        result = on_event(event, MagicMock())

        # The record must describe what was committed, not be lost with the
        # exception that stopped the fan-out.
        writes = _provisioner_writes(mock_client)
        assert len(writes) == 1
        assert writes[0].kwargs["Item"]["managed_resources"] == {"L": [{"S": "gpt-4"}]}

        # A rollback of a stack whose config is already applied is the worst
        # available outcome (#563): SUCCESS carrying the error, not FAILED.
        body = json.loads(mock_urlopen.call_args.args[0].data)
        assert body["Status"] == "SUCCESS"

        assert result["status"] == "applied"
        assert any("user-1/gpt-4" in e for e in result["errors"])

    def test_cli_records_state_and_reports_errors(
        self, mock_handler_boto3, mock_applier_boto3, mock_urlopen
    ):
        """`_handle_cli` shares the three-step ordering, so it loses the record
        the same way — it just lacks the rollback-vs-reality contradiction."""
        mock_client = _corrupt_system_schedule_client(mock_handler_boto3, mock_applier_boto3)

        event = {
            "action": "apply",
            "table_name": "test-table",
            "namespace_id": "ns123",
            "manifest": {
                "namespace": "test-ns",
                "resources": {"gpt-4": {"limits": {"rpm": {"capacity": 2000}}}},
            },
        }

        result = on_event(event, MagicMock())

        assert len(_provisioner_writes(mock_client)) == 1
        assert result["status"] == "applied"
        assert any("user-1/gpt-4" in e for e in result["errors"])

    def test_one_bad_change_does_not_abandon_the_others(
        self, mock_handler_boto3, mock_applier_boto3, mock_urlopen
    ):
        """Per-change guarding, the analogue of `FanoutIncomplete.stamped`:
        the corrupt entity is reported and every other entity still syncs."""
        mock_client = MagicMock()

        def _get_item(*_args, **kwargs):
            key = (kwargs["Key"]["PK"]["S"], kwargs["Key"]["SK"]["S"])
            if key == (pk_system("ns123"), sk_provisioner()):
                return {
                    "Item": {
                        "managed_system": {"BOOL": False},
                        "managed_resources": {"L": []},
                        "managed_entities": {
                            "M": {
                                "user-1": {"L": [{"S": "gpt-4"}]},
                                "user-2": {"L": [{"S": "claude-3"}]},
                            }
                        },
                    }
                }
            if key == (pk_system("ns123"), sk_config()):
                return _CORRUPT_SYSTEM_CONFIG
            if key == (pk_resource("ns123", "claude-3"), sk_config()):
                return {
                    "Item": {
                        "l_tpm_cp": {"N": "500"},
                        "l_tpm_ra": {"N": "500"},
                        "l_tpm_rp": {"N": "60"},
                    }
                }
            return {}

        mock_client.get_item.side_effect = _get_item
        mock_client.delete_item.return_value = _DELETED_ENTITY_CONFIG
        mock_client.query.return_value = {
            "Items": [{"PK": {"S": "ns123/BUCKET#user-2#claude-3#0"}}]
        }
        mock_handler_boto3.client.return_value = mock_client
        mock_applier_boto3.client.return_value = mock_client

        result = on_event(
            {
                "action": "apply",
                "table_name": "test-table",
                "namespace_id": "ns123",
                "manifest": {"namespace": "test-ns"},
            },
            MagicMock(),
        )

        assert [e for e in result["errors"] if "user-1/gpt-4" in e]
        assert not [e for e in result["errors"] if "user-2/claude-3" in e]
        # user-2's bucket was still reconciled down to the resource default.
        assert [
            c
            for c in mock_client.update_item.call_args_list
            if c.kwargs["Key"]["PK"]["S"] == "ns123/BUCKET#user-2#claude-3#0"
        ]
        assert len(_provisioner_writes(mock_client)) == 1

    def test_disable_fanout_failure_is_reported_not_raised(
        self, mock_handler_boto3, mock_applier_boto3, mock_urlopen
    ):
        """The ADR-125 stamp fan-out is guarded on the same terms.

        It runs before the param sync and after the same committed config
        writes, so an exception escaping it loses the record identically.
        `_decode_limits` is not its failure mode — a throttled GSI3 query is —
        but the ordering hazard is the fan-out's, not any one exception's.
        """
        mock_client = _setup_client(mock_handler_boto3, mock_applier_boto3)

        def _query(**kwargs):
            if "IndexName" not in kwargs:
                return {"Items": []}  # the pending fan-outs
            raise RuntimeError("ProvisionedThroughputExceeded")

        mock_client.query.side_effect = _query

        result = on_event(
            {
                "action": "apply",
                "table_name": "test-table",
                "namespace_id": "ns123",
                "manifest": {
                    "namespace": "test-ns",
                    "resources": {
                        "gpt-4": {"disabled": True, "limits": {"rpm": {"capacity": 1000}}}
                    },
                },
            },
            MagicMock(),
        )

        assert result["status"] == "applied"
        assert any("gpt-4" in e and "ProvisionedThroughputExceeded" in e for e in result["errors"])
        writes = _provisioner_writes(mock_client)
        assert len(writes) == 1
        # Its pending item, written ahead, is kept for the next apply, which
        # would otherwise see no change: written, never deleted.
        pending_sk = "#PROVISIONER#PENDING#disabled#resource#gpt-4"
        assert pending_sk in [
            c.kwargs["Item"]["SK"]["S"] for c in mock_client.put_item.call_args_list
        ]
        assert pending_sk not in [
            c.kwargs["Key"]["SK"]["S"] for c in mock_client.delete_item.call_args_list
        ]


class TestResetAfterVersionGateThroughTheHandler:
    """The #638 gate runs before anything is written, on both entry points.

    Moto-backed end to end: a refusal must leave the table untouched, and
    CloudFormation must hear FAILED with a message it can show.
    """

    TABLE = "prov-gate-handler"

    @pytest.fixture
    def client(self, mock_dynamodb):
        import boto3

        from zae_limiter.sync_repository import SyncRepository

        setup = SyncRepository(name=self.TABLE, region="us-east-1", _skip_deprecation_warning=True)
        setup.create_table()
        setup.close()
        return boto3.client("dynamodb", region_name="us-east-1")

    def _stamp(self, client, lambda_version):
        client.put_item(
            TableName=self.TABLE,
            Item={
                "PK": {"S": "_/SYSTEM#"},
                "SK": {"S": "#VERSION"},
                "schema_version": {"S": "0.10.0"},
                "lambda_version": {"S": lambda_version},
                "client_min_version": {"S": "0.0.0"},
            },
        )

    def _keys(self, client):
        return sorted(
            (i["PK"]["S"], i["SK"]["S"]) for i in client.scan(TableName=self.TABLE)["Items"]
        )

    def _cli_event(self):
        return {
            "action": "apply",
            "table_name": self.TABLE,
            "namespace_id": "ns123",
            "manifest": {
                "namespace": "test-ns",
                "resources": {
                    "gpt-4": {
                        "limits": {"session": {"capacity": 10, "reset_after_seconds": 18_000}}
                    }
                },
            },
        }

    @patch("zae_limiter_provisioner.handler.urllib.request.urlopen")
    def test_cfn_hears_failed_and_nothing_is_written(self, mock_urlopen, client):
        self._stamp(client, "0.14.0")
        before = self._keys(client)
        event = {
            "RequestType": "Create",
            "ResourceProperties": {
                "ServiceToken": "arn:aws:lambda:us-east-1:123:function:test",
                "TableName": self.TABLE,
                "Namespace": "test-ns",
                "NamespaceId": "ns123",
                "Resources": {
                    "gpt-4": {
                        "Limits": {"session": {"Capacity": "10", "ResetAfterSeconds": "18000"}}
                    }
                },
            },
            "ResponseURL": "https://cfn-response.example.com",
            "StackId": "arn:aws:cloudformation:us-east-1:123:stack/test/guid",
            "RequestId": "test-request-id",
            "LogicalResourceId": "TenantLimits",
        }
        with patch("zae_limiter_provisioner.applier.__version__", "0.15.0"):
            with pytest.raises(VersionMismatchError):
                on_event(event, MagicMock())

        body = json.loads(mock_urlopen.call_args.args[0].data)
        assert body["Status"] == "FAILED"
        assert "zae-limiter upgrade" in body["Reason"]
        assert self._keys(client) == before

    def test_cli_apply_raises_and_nothing_is_written(self, client):
        self._stamp(client, "0.14.0")
        before = self._keys(client)
        with patch("zae_limiter_provisioner.applier.__version__", "0.15.0"):
            with pytest.raises(VersionMismatchError, match="zae-limiter upgrade"):
                on_event(self._cli_event(), MagicMock())
        assert self._keys(client) == before

    def test_cli_apply_succeeds_once_the_lambdas_read_it(self, client):
        self._stamp(client, "0.15.0")
        with patch("zae_limiter_provisioner.applier.__version__", "0.15.0"):
            result = on_event(self._cli_event(), MagicMock())
        assert result["status"] == "applied"
        assert result["errors"] == []
        config = client.get_item(
            TableName=self.TABLE,
            Key={"PK": {"S": "ns123/RESOURCE#gpt-4"}, "SK": {"S": "#CONFIG"}},
        )["Item"]
        assert config["w_session_rsa"]["N"] == "18000"


class TestCascadeFanoutChanges:
    """Only levels whose cascade policy changed are fanned out (ADR-146, decision A)."""

    @patch("zae_limiter_provisioner.handler.fanout_cascade")
    @patch("zae_limiter_provisioner.handler.boto3")
    def test_each_changed_level_is_fanned_out_once(self, mock_boto3, mock_fanout):
        from zae_limiter_provisioner.handler import _fanout_cascade_changes

        errors, failed = _fanout_cascade_changes(
            "t", "ns", [("resource", "llm"), ("entity", "u1/_default_")]
        )

        assert (errors, failed) == ([], [])
        client = mock_boto3.client.return_value
        assert mock_fanout.call_args_list[0].args == (client, "t", "ns")
        assert mock_fanout.call_args_list[0].kwargs == {"resource": "llm"}
        assert mock_fanout.call_args_list[1].kwargs == {
            "entity_id": "u1",
            "resource": "_default_",
        }

    @patch("zae_limiter_provisioner.handler.boto3")
    def test_nothing_changed_means_no_work(self, mock_boto3):
        from zae_limiter_provisioner.handler import _fanout_cascade_changes

        assert _fanout_cascade_changes("t", "ns", []) == ([], [])
        mock_boto3.client.assert_not_called()

    @patch("zae_limiter_provisioner.handler.fanout_cascade", side_effect=RuntimeError("boom"))
    @patch("zae_limiter_provisioner.handler.boto3")
    def test_a_failure_is_reported_and_kept_for_retry(self, mock_boto3, mock_fanout):
        from zae_limiter_provisioner.handler import _fanout_cascade_changes

        errors, failed = _fanout_cascade_changes("t", "ns", [("resource", "llm")])

        assert errors == ["cascade fan-out resource llm: boom (retried on the next apply)"]
        assert failed == [("resource", "llm")]

    @patch("zae_limiter_provisioner.handler._fanout_soft_changes", return_value=[])
    @patch("zae_limiter_provisioner.handler.write_ahead", return_value=set())
    @patch("zae_limiter_provisioner.handler.claim", return_value=[])
    @patch("zae_limiter_provisioner.handler._write_provisioner_state")
    @patch("zae_limiter_provisioner.handler._sync_bucket_param_changes", return_value=([], []))
    @patch("zae_limiter_provisioner.handler._fanout_disabled_changes", return_value=([], []))
    @patch("zae_limiter_provisioner.handler._fanout_cascade_changes")
    @patch("zae_limiter_provisioner.handler.apply_changes")
    @patch("zae_limiter_provisioner.handler.require_cascade_policy_readers")
    @patch("zae_limiter_provisioner.handler.require_reset_after_readers")
    @patch("zae_limiter_provisioner.handler.boto3")
    def test_a_v016_records_pending_list_is_retried_and_dropped(
        self,
        _boto3,
        _gate1,
        mock_gate,
        mock_apply,
        mock_fanout,
        _disabled,
        _sync,
        mock_write,
        _claim,
        mock_ahead,
        _soft,
    ):
        from zae_limiter_provisioner.applier import ApplyResult
        from zae_limiter_provisioner.handler import _apply_and_record
        from zae_limiter_provisioner.manifest import LimitsManifest

        mock_apply.return_value = ApplyResult(
            cascade_changed=[("resource", "llm"), ("entity", "u1/llm")]
        )
        mock_fanout.return_value = (["err"], [("entity", "u1/llm")])
        changes = [Change(action="update", level="resource", target="llm", data={})]

        result = _apply_and_record(
            LimitsManifest.from_dict({"namespace": "x"}),
            changes,
            "t",
            "ns",
            [("entity", "old/gpt-4"), ("resource", "llm")],
        )

        mock_gate.assert_called_once_with(changes, "t")
        # Every owed cascade fan-out runs once, on its own, through the
        # table-driven retry (`resource llm` is both this apply's and a
        # leftover); resources first.
        assert [c.args for c in mock_fanout.call_args_list] == [
            ("t", "ns", [("resource", "llm")]),
            ("t", "ns", [("entity", "u1/llm")]),
            ("t", "ns", [("entity", "old/gpt-4")]),
        ]
        assert "err" in result.errors
        # The legacy entries become pending items before anything is written,
        # so a failure (or a crash) leaves them for the next apply; the
        # record itself no longer carries the list.
        owed = set(mock_ahead.call_args.args[4])
        assert {("cascade", "entity", "old/gpt-4"), ("cascade", "resource", "llm")} <= owed
        assert "cascade_pending" not in mock_write.call_args.args[2]


class TestCascadePendingState:
    """A v0.16 #PROVISIONER record carries cascade fan-outs left to retry (ADR-146)."""

    TABLE = "prov-pending"

    @pytest.fixture
    def table(self, mock_dynamodb):
        from zae_limiter.sync_repository import SyncRepository

        repo = SyncRepository(name=self.TABLE, region="us-east-1", _skip_deprecation_warning=True)
        repo.create_table()
        repo.close()

    def test_a_legacy_list_is_read(self, table):
        import boto3

        from zae_limiter_provisioner.handler import _read_provisioner_state

        boto3.client("dynamodb", region_name="us-east-1").put_item(
            TableName=self.TABLE,
            Item={
                "PK": {"S": pk_system("ns")},
                "SK": {"S": sk_provisioner()},
                "cascade_pending": {
                    "L": [
                        {"M": {"level": {"S": "resource"}, "target": {"S": "openai/gpt-4"}}},
                        {"M": {"level": {"S": "entity"}, "target": {"S": "u1/_default_"}}},
                    ]
                },
            },
        )

        assert _read_provisioner_state(self.TABLE, "ns")["cascade_pending"] == [
            ("resource", "openai/gpt-4"),
            ("entity", "u1/_default_"),
        ]

    def test_the_record_no_longer_carries_one(self, table):
        import boto3

        from zae_limiter_provisioner.handler import (
            _read_provisioner_state,
            _write_provisioner_state,
        )

        _write_provisioner_state(self.TABLE, "ns", {"cascade_pending": [("resource", "x")]})
        item = boto3.client("dynamodb", region_name="us-east-1").get_item(
            TableName=self.TABLE,
            Key={"PK": {"S": pk_system("ns")}, "SK": {"S": sk_provisioner()}},
        )["Item"]
        assert "cascade_pending" not in item
        assert _read_provisioner_state(self.TABLE, "ns")["cascade_pending"] == []


class TestProvisionerItemsArePurgeable:
    """Every item the provisioner writes is found through GSI4 (`purge_namespace`).

    GSI4 is keyed ``GSI4PK`` + ``GSI4SK``, and DynamoDB leaves an item missing
    either key attribute out of the index. Config items and the #PROVISIONER
    record carried ``GSI4PK`` only, so a purge left every limit config a
    manifest wrote, and its state record, behind.
    """

    TABLE = "prov-gsi4"
    NS = "ns-gsi4"

    @pytest.fixture
    def client(self, mock_dynamodb):
        import boto3

        from zae_limiter.sync_repository import SyncRepository

        repo = SyncRepository(name=self.TABLE, region="us-east-1", _skip_deprecation_warning=True)
        repo.create_table()
        repo.close()
        return boto3.client("dynamodb", region_name="us-east-1")

    def _gsi4_keys(self, client):
        from zae_limiter.schema import GSI4_NAME

        response = client.query(
            TableName=self.TABLE,
            IndexName=GSI4_NAME,
            KeyConditionExpression="GSI4PK = :ns",
            ExpressionAttributeValues={":ns": {"S": self.NS}},
        )
        return {(i["PK"]["S"], i["SK"]["S"]) for i in response["Items"]}

    def test_the_state_record_is_indexed(self, client):
        from zae_limiter.schema import pk_system, sk_provisioner
        from zae_limiter_provisioner.handler import _write_provisioner_state

        _write_provisioner_state(self.TABLE, self.NS, {"managed_system": True})

        assert (pk_system(self.NS), sk_provisioner()) in self._gsi4_keys(client)

    @pytest.mark.parametrize(
        "level,target",
        [("system", None), ("resource", "gpt-4"), ("entity", "u1/gpt-4")],
    )
    def test_every_config_level_is_indexed(self, client, level, target):
        from zae_limiter_provisioner.applier import apply_changes
        from zae_limiter_provisioner.differ import Change

        rpm = {"capacity": 100, "refill_amount": 100, "refill_period": 60}
        change = Change(action="create", level=level, target=target, data={"limits": {"rpm": rpm}})
        apply_changes([change], self.TABLE, self.NS, client=client)

        configs = {key for key in self._gsi4_keys(client) if key[1].startswith("#CONFIG")}
        assert len(configs) == 1


class TestSystemDisabledThroughTheHandler:
    """#693: `disabled` on `system` fails both entry points before any write.

    ADR-125 has no system-level disable, so the value used to be dropped and the
    apply reported success while nothing was disabled.
    """

    TABLE = "prov-system-disabled"

    @pytest.fixture
    def client(self, mock_dynamodb):
        import boto3

        from zae_limiter.sync_repository import SyncRepository

        setup = SyncRepository(name=self.TABLE, region="us-east-1", _skip_deprecation_warning=True)
        setup.create_table()
        setup.close()
        return boto3.client("dynamodb", region_name="us-east-1")

    def _keys(self, client):
        return sorted(
            (i["PK"]["S"], i["SK"]["S"]) for i in client.scan(TableName=self.TABLE)["Items"]
        )

    @patch("zae_limiter_provisioner.handler.urllib.request.urlopen")
    def test_cfn_hears_failed_and_nothing_is_written(self, mock_urlopen, client):
        before = self._keys(client)
        event = {
            "RequestType": "Create",
            "ResourceProperties": {
                "ServiceToken": "arn:aws:lambda:us-east-1:123:function:test",
                "TableName": self.TABLE,
                "Namespace": "test-ns",
                "NamespaceId": "ns123",
                "System": {"Disabled": "true", "Limits": {"rpm": {"Capacity": "10"}}},
            },
            "ResponseURL": "https://cfn-response.example.com",
            "StackId": "arn:aws:cloudformation:us-east-1:123:stack/test/guid",
            "RequestId": "test-request-id",
            "LogicalResourceId": "TenantLimits",
        }
        with pytest.raises(ValueError):
            on_event(event, MagicMock())

        body = json.loads(mock_urlopen.call_args.args[0].data)
        assert body["Status"] == "FAILED"
        assert "'disabled' is not supported at the system level" in body["Reason"]
        assert self._keys(client) == before

    @pytest.mark.parametrize("action", ["plan", "apply", "diff"])
    def test_cli_raises_and_nothing_is_written(self, client, action):
        before = self._keys(client)
        event = {
            "action": action,
            "table_name": self.TABLE,
            "namespace_id": "ns123",
            "manifest": {
                "namespace": "test-ns",
                "system": {"disabled": False, "limits": {"rpm": {"capacity": 10}}},
            },
        }
        with pytest.raises(ValueError, match="'disabled' is not supported at the system level"):
            on_event(event, MagicMock())
        assert self._keys(client) == before


class _Killed(BaseException):
    """The Lambda runtime killing an invocation: no ``except Exception`` catches it."""


class _CountingClient:
    """A real boto3 client that counts the calls made through it."""

    def __init__(self, client):
        self._client = client
        self.calls: dict[str, int] = {}

    def __getattr__(self, name):
        attr = getattr(self._client, name)
        if not callable(attr):
            return attr

        def _counted(*args, **kwargs):
            self.calls[name] = self.calls.get(name, 0) + 1
            return attr(*args, **kwargs)

        return _counted


class TestApplyCostsWhatChanged:
    """A re-apply writes and fans out only the levels that changed.

    The reported case: a manifest of ~1,200 entities, one entry added, logged
    "Applied: 1 created, 1178 updated" — every declared level rewritten and
    fanned out, serially, on every apply.
    """

    TABLE = "prov-change-only"
    ENTITIES = 40

    @pytest.fixture
    def counting(self, mock_dynamodb):
        import boto3

        from zae_limiter.sync_repository import SyncRepository

        setup = SyncRepository(name=self.TABLE, region="us-east-1", _skip_deprecation_warning=True)
        setup.create_table()
        setup.close()
        client = _CountingClient(boto3.client("dynamodb", region_name="us-east-1"))
        with patch("boto3.client", return_value=client):
            yield client

    def _manifest(self, entities: int, **resource):
        return {
            "namespace": "test-ns",
            "resources": {"gpt-4": {"limits": {"rpm": {"capacity": 1000}}, **resource}},
            "entities": {
                f"user-{i}": {"resources": {"gpt-4": {"limits": {"rpm": {"capacity": 10}}}}}
                for i in range(entities)
            },
        }

    def _apply(self, manifest, action="apply"):
        return on_event(
            {
                "action": action,
                "table_name": self.TABLE,
                "namespace_id": "ns123",
                "manifest": manifest,
            },
            MagicMock(),
        )

    def test_adding_one_entry_applies_one_change(self, counting):
        first = self._apply(self._manifest(self.ENTITIES))
        assert first["created"] == self.ENTITIES + 1
        assert first["unchanged"] == 0

        counting.calls.clear()
        result = self._apply(self._manifest(self.ENTITIES + 1))

        assert result["changes"] == [
            {"action": "create", "level": "entity", "target": f"user-{self.ENTITIES}/gpt-4"}
        ]
        assert (result["created"], result["updated"], result["deleted"]) == (1, 0, 0)
        assert result["unchanged"] == self.ENTITIES + 1
        assert result["errors"] == []
        # The new config item, its sync written ahead as a pending item, and
        # the #PROVISIONER record; nothing else.
        assert counting.calls["put_item"] == 3
        # The pending sync, removed once it landed.
        assert counting.calls["delete_item"] == 1
        # The pending-items query, then the new entity's bucket sync (two passes).
        assert counting.calls.get("query", 0) == 3
        assert counting.calls.get("update_item", 0) == 0

    def test_an_identical_reapply_writes_only_the_record(self, counting):
        self._apply(self._manifest(self.ENTITIES))
        counting.calls.clear()

        result = self._apply(self._manifest(self.ENTITIES))

        assert result["changes"] == []
        assert result["unchanged"] == self.ENTITIES + 1
        assert counting.calls["put_item"] == 1
        # Only the pending-items query: no bucket is discovered.
        assert counting.calls.get("query", 0) == 1
        assert counting.calls.get("delete_item", 0) == 0

    def test_plan_lists_only_what_would_change(self, counting):
        self._apply(self._manifest(self.ENTITIES))
        manifest = self._manifest(self.ENTITIES)
        manifest["entities"]["user-3"]["resources"]["gpt-4"]["limits"]["rpm"]["capacity"] = 20

        result = self._apply(manifest, action="plan")

        assert result["changes"] == [
            {"action": "update", "level": "entity", "target": "user-3/gpt-4"}
        ]
        assert result["unchanged"] == self.ENTITIES

    def test_a_config_item_written_before_gsi4sk_is_backfilled(self, counting):
        """An item a pre-#778 provisioner wrote lacks GSI4SK, so GSI4 misses it.

        It is otherwise exactly what the manifest builds. The change-only
        pre-read compares the whole item, so the missing key alone is a
        difference: the apply rewrites it and the item becomes indexed.
        """
        key = {"PK": {"S": pk_entity("ns123", "user-0")}, "SK": {"S": sk_config("gpt-4")}}
        self._apply(self._manifest(1))
        item = counting.get_item(TableName=self.TABLE, Key=key)["Item"]
        assert "GSI4SK" in item
        counting.put_item(
            TableName=self.TABLE, Item={k: v for k, v in item.items() if k != "GSI4SK"}
        )

        result = self._apply(self._manifest(1))

        assert result["changes"] == [
            {"action": "update", "level": "entity", "target": "user-0/gpt-4"}
        ]
        assert counting.get_item(TableName=self.TABLE, Key=key)["Item"]["GSI4SK"] == item["GSI4SK"]

    def test_a_level_changed_out_of_band_is_restored(self, counting):
        self._apply(self._manifest(2))
        counting.update_item(
            TableName=self.TABLE,
            Key={"PK": {"S": pk_entity("ns123", "user-1")}, "SK": {"S": sk_config("gpt-4")}},
            UpdateExpression="SET l_rpm_cp = :c",
            ExpressionAttributeValues={":c": {"N": "500"}},
        )

        result = self._apply(self._manifest(2))

        assert result["changes"] == [
            {"action": "update", "level": "entity", "target": "user-1/gpt-4"}
        ]
        item = counting.get_item(
            TableName=self.TABLE,
            Key={"PK": {"S": pk_entity("ns123", "user-1")}, "SK": {"S": sk_config("gpt-4")}},
        )["Item"]
        assert item["l_rpm_cp"] == {"N": "10"}

    def test_a_failed_fanout_is_retried_by_an_unchanged_reapply(self, counting):
        """Change-only fan-out must not lose a fan-out that failed."""
        manifest = self._manifest(1, disabled=True)
        with patch(
            "zae_limiter_provisioner.handler.fanout_resource", side_effect=RuntimeError("boom")
        ):
            first = self._apply(manifest)
        assert any("disable fan-out resource gpt-4" in e for e in first["errors"])

        with patch("zae_limiter_provisioner.handler.fanout_resource") as retry:
            second = self._apply(manifest)
        assert second["changes"] == []
        assert second["errors"] == []
        retry.assert_called_once()
        assert retry.call_args.args[3:] == ("gpt-4", True)

        # Cleared; a declared `disabled` is still re-asserted on every apply,
        # skipping buckets already stamped (the missed-bucket repair).
        with patch("zae_limiter_provisioner.handler.fanout_resource") as third:
            self._apply(manifest)
        third.assert_called_once()
        assert third.call_args.kwargs == {"skip_matching": True}

    def test_a_failed_sync_for_a_dropped_entity_is_retried_as_a_delete(self, counting):
        self._apply(self._manifest(2))
        with patch(
            "zae_limiter_provisioner.handler.sync_bucket_params", side_effect=RuntimeError("boom")
        ):
            first = self._apply(self._manifest(1))
        assert any("bucket param sync entity user-1/gpt-4" in e for e in first["errors"])

        with patch("zae_limiter_provisioner.handler.sync_bucket_params") as retry:
            self._apply(self._manifest(1))
        retry.assert_called_once()
        kwargs = retry.call_args.kwargs
        assert (kwargs["entity_id"], kwargs["resource"]) == ("user-1", "gpt-4")
        # Reconciled to the resource default, which persists with a TTL.
        assert kwargs["limits"]["rpm"]["capacity"] == 1000
        assert kwargs["ttl_multiplier"] == 7


class TestPendingItems:
    """One item per owed fan-out, owned by the token of the apply holding it."""

    TABLE = "prov-pending-items"

    @pytest.fixture
    def client(self, mock_dynamodb):
        import boto3

        from zae_limiter.sync_repository import SyncRepository

        repo = SyncRepository(name=self.TABLE, region="us-east-1", _skip_deprecation_warning=True)
        repo.create_table()
        repo.close()
        return boto3.client("dynamodb", region_name="us-east-1")

    def test_written_items_read_back(self, client):
        from zae_limiter_provisioner.pending import Pending, read_pending, write_ahead

        written = write_ahead(
            client,
            self.TABLE,
            "ns",
            "tok-a",
            [("disabled", "resource", "gpt-4"), ("sync", "entity", "u1/_default_")] * 2,
        )

        assert written == {("disabled", "resource", "gpt-4"), ("sync", "entity", "u1/_default_")}
        items = sorted(read_pending(client, self.TABLE, "ns"), key=lambda p: p.key)
        assert [(p.key, p.token) for p in items] == [
            (("disabled", "resource", "gpt-4"), "tok-a"),
            (("sync", "entity", "u1/_default_"), "tok-a"),
        ]
        assert all(isinstance(p, Pending) and p.lease_until_ms for p in items)
        assert read_pending(client, self.TABLE, "other") == []

    def test_each_item_is_found_by_purge_namespace(self, client):
        from zae_limiter_provisioner.pending import sk_pending, write_ahead

        write_ahead(client, self.TABLE, "ns", "tok-a", [("cascade", "resource", "gpt-4")])
        item = client.get_item(
            TableName=self.TABLE,
            Key={
                "PK": {"S": pk_system("ns")},
                "SK": {"S": sk_pending("cascade", "resource", "gpt-4")},
            },
        )["Item"]
        assert item["GSI4PK"] == {"S": "ns"}

    def test_each_item_is_indexed_in_gsi4(self, client):
        """Review finding 3: GSI4 indexes only items carrying both GSI4PK and GSI4SK."""
        from zae_limiter.schema import GSI4_NAME
        from zae_limiter_provisioner.pending import sk_pending, write_ahead

        write_ahead(client, self.TABLE, "ns", "tok-a", [("cascade", "resource", "gpt-4")])
        found = client.query(
            TableName=self.TABLE,
            IndexName=GSI4_NAME,
            KeyConditionExpression="GSI4PK = :ns",
            ExpressionAttributeValues={":ns": {"S": "ns"}},
        )["Items"]

        assert [(i["PK"]["S"], i["SK"]["S"]) for i in found] == [
            (pk_system("ns"), sk_pending("cascade", "resource", "gpt-4"))
        ]

    def test_a_release_by_another_token_leaves_the_item(self, client):
        from zae_limiter_provisioner.pending import read_pending, release, write_ahead

        key = ("sync", "entity", "u1/gpt-4")
        write_ahead(client, self.TABLE, "ns", "tok-a", [key], lease_until_ms=0)
        # A later apply takes it on once the first one's lease has run out.
        assert write_ahead(client, self.TABLE, "ns", "tok-b", [key]) == {key}

        release(client, self.TABLE, "ns", "tok-a", key)
        assert [p.token for p in read_pending(client, self.TABLE, "ns")] == ["tok-b"]

        release(client, self.TABLE, "ns", "tok-b", key)
        assert read_pending(client, self.TABLE, "ns") == []

    def test_a_live_lease_is_not_taken_over(self, client):
        """Re-review #2: an item leased to a live apply stays that apply's."""
        from zae_limiter_provisioner.pending import claim, read_pending, write_ahead

        key = ("sync", "entity", "u1/gpt-4")
        write_ahead(client, self.TABLE, "ns", "tok-a", [key])

        assert write_ahead(client, self.TABLE, "ns", "tok-b", [key]) == set()
        assert (
            claim(client, self.TABLE, "ns", "tok-b", read_pending(client, self.TABLE, "ns")) == []
        )
        assert [p.token for p in read_pending(client, self.TABLE, "ns")] == ["tok-a"]

    def test_an_expired_lease_is_claimed_and_renewed(self, client):
        from zae_limiter_provisioner.pending import claim, now_ms, read_pending, write_ahead

        key = ("sync", "entity", "u1/gpt-4")
        write_ahead(client, self.TABLE, "ns", "tok-a", [key], lease_until_ms=now_ms() - 1)
        lease = now_ms() + 60_000

        (claimed,) = claim(
            client, self.TABLE, "ns", "tok-b", read_pending(client, self.TABLE, "ns"), lease
        )

        assert claimed.key == key
        (item,) = read_pending(client, self.TABLE, "ns")
        assert (item.token, item.lease_until_ms) == ("tok-b", lease)

    def test_a_finished_apply_ends_its_lease_on_what_it_still_holds(self, client):
        from zae_limiter_provisioner.pending import expire, now_ms, read_pending, write_ahead

        key = ("sync", "entity", "u1/gpt-4")
        write_ahead(client, self.TABLE, "ns", "tok-a", [key])

        expire(client, self.TABLE, "ns", "tok-b", [key])  # not its item
        (item,) = read_pending(client, self.TABLE, "ns")
        assert not item.claimable(now_ms())

        expire(client, self.TABLE, "ns", "tok-a", [key])
        (item,) = read_pending(client, self.TABLE, "ns")
        assert item.claimable(now_ms())

    def test_a_failed_expire_is_logged_not_raised(self, client):
        from zae_limiter_provisioner.pending import expire

        failing = MagicMock()
        failing.exceptions = client.exceptions
        failing.update_item.side_effect = RuntimeError("throttled")

        expire(failing, self.TABLE, "ns", "tok", [("sync", "entity", "u1/gpt-4")])

    def test_the_lease_follows_the_invocations_own_timeout(self):
        from zae_limiter_provisioner.pending import (
            LEASE_MARGIN_MS,
            MAX_RUN_MS,
            deadline_for,
            now_ms,
        )

        before = now_ms()
        assert deadline_for(300_000) - before - LEASE_MARGIN_MS in range(300_000, 301_000)
        assert deadline_for(None) - before - LEASE_MARGIN_MS in range(MAX_RUN_MS, MAX_RUN_MS + 1000)

    def test_the_read_follows_every_page(self):
        from zae_limiter_provisioner.pending import Pending, read_pending

        def item(target):
            return {
                "kind": {"S": "sync"},
                "level": {"S": "entity"},
                "target": {"S": target},
                "token": {"S": "t"},
            }

        client = MagicMock()
        client.query.side_effect = [
            {"Items": [item("u1/a"), {"SK": {"S": "malformed"}}], "LastEvaluatedKey": {"k": 1}},
            {"Items": [item("u2/a")]},
        ]

        assert read_pending(client, "t", "ns") == [
            Pending("sync", "entity", "u1/a", "t"),
            Pending("sync", "entity", "u2/a", "t"),
        ]
        assert client.query.call_args_list[1].kwargs["ExclusiveStartKey"] == {"k": 1}

    def test_a_failed_release_is_logged_not_raised(self, client):
        """The fan-out landed; a leftover item only costs the next apply a retry."""
        from zae_limiter_provisioner.pending import release

        failing = MagicMock()
        failing.exceptions = client.exceptions
        failing.delete_item.side_effect = RuntimeError("throttled")

        release(failing, self.TABLE, "ns", "tok", ("sync", "entity", "u1/gpt-4"))

    def test_a_claim_takes_only_what_it_read(self, client):
        from zae_limiter_provisioner.pending import Pending, claim, read_pending, write_ahead

        for target in ("u1/gpt-4", "u2/gpt-4"):
            write_ahead(
                client, self.TABLE, "ns", "tok-a", [("sync", "entity", target)], lease_until_ms=0
            )
        seen = read_pending(client, self.TABLE, "ns")
        # Another apply claims u2's item after we read it.
        claim(client, self.TABLE, "ns", "tok-c", [p for p in seen if p.target == "u2/gpt-4"])

        claimed = claim(client, self.TABLE, "ns", "tok-b", seen)

        assert [p.target for p in claimed] == ["u1/gpt-4"]
        tokens = {p.target: p.token for p in read_pending(client, self.TABLE, "ns")}
        assert tokens == {"u1/gpt-4": "tok-b", "u2/gpt-4": "tok-c"}
        # An item removed since it was read is not claimed back into existence.
        gone = Pending("disabled", "resource", "gone", "tok-a")
        assert claim(client, self.TABLE, "ns", "tok-b", [gone]) == []
        assert len(read_pending(client, self.TABLE, "ns")) == 2


class TestRemainingMs:
    """The lease reads the invocation's own remaining time (re-review #2)."""

    def test_a_lambda_context_gives_its_remaining_time(self):
        from zae_limiter_provisioner.handler import _remaining_ms

        context = MagicMock()
        context.get_remaining_time_in_millis.return_value = 300_000
        assert _remaining_ms(context) == 300_000

    @pytest.mark.parametrize(
        "context",
        [
            None,
            object(),
            MagicMock(get_remaining_time_in_millis=MagicMock(side_effect=RuntimeError)),
            MagicMock(get_remaining_time_in_millis=MagicMock(return_value=True)),
            MagicMock(get_remaining_time_in_millis=MagicMock(return_value="300000")),
        ],
    )
    def test_anything_else_says_nothing(self, context):
        from zae_limiter_provisioner.handler import _remaining_ms

        assert _remaining_ms(context) is None


class TestOwedFanouts:
    """Every owed fan-out, from every source, once each."""

    def test_each_source_once(self):
        from zae_limiter_provisioner.applier import ApplyResult
        from zae_limiter_provisioner.handler import _owed_fanouts

        reported = Change("update", "entity", "u1/gpt-4", {})
        unreported = Change("update", "entity", "u2/gpt-4", {})
        result = ApplyResult(
            limits_changed=[reported],
            disabled_changed=[reported],
            cascade_changed=[("resource", "gpt-4")],
        )

        owed = _owed_fanouts(
            result,
            [("sync", reported), ("sync", unreported), ("cascade", unreported)],
            [("sync", "entity", "u3/gpt-4"), ("sync", "entity", "u2/gpt-4")],
        )

        assert owed == [
            ("sync", "entity", "u1/gpt-4"),
            ("sync", "entity", "u2/gpt-4"),
            ("cascade", "entity", "u2/gpt-4"),
            ("disabled", "entity", "u1/gpt-4"),
            ("cascade", "resource", "gpt-4"),
            ("sync", "entity", "u3/gpt-4"),
        ]


class TestRetryFromTable:
    """A leftover fan-out is retried from its level's live config (finding 1)."""

    TABLE = "prov-retry-from-table"

    @pytest.fixture
    def client(self, mock_dynamodb):
        import boto3

        from zae_limiter.sync_repository import SyncRepository
        from zae_limiter_provisioner.applier import apply_changes

        repo = SyncRepository(name=self.TABLE, region="us-east-1", _skip_deprecation_warning=True)
        repo.create_table()
        repo.close()
        client = _CountingClient(boto3.client("dynamodb", region_name="us-east-1"))
        apply_changes(
            [
                Change(
                    "create",
                    "resource",
                    "gpt-4",
                    {"disabled": True, "cascade": False, "limits": _limits(10)},
                ),
                Change("create", "entity", "u1/gpt-4", {"limits": _limits(5)}),
            ],
            self.TABLE,
            "ns",
            client=client,
        )
        client.calls.clear()
        return client

    def _retry(self, client, key):
        from zae_limiter_provisioner.handler import _retry_from_table

        done = MagicMock()
        errors = _retry_from_table(client, self.TABLE, "ns", key, done)
        return errors, done

    def test_a_sync_is_retried_from_the_live_limits(self, client):
        with patch("zae_limiter_provisioner.handler._sync_bucket_param_changes") as sync:
            sync.return_value = ([], [])
            errors, done = self._retry(client, ("sync", "entity", "u1/gpt-4"))
        assert errors == []
        (change,) = sync.call_args.args[2]
        assert (change.action, change.data) == ("update", {"limits": _limits(5)})
        done.assert_called_once_with("sync", "entity", "u1/gpt-4")
        # Read before and after the fan-out, both strongly consistent.
        assert client.calls == {"get_item": 2}

    def test_a_disabled_retry_carries_the_live_flags(self, client):
        with patch("zae_limiter_provisioner.handler._fanout_disabled_changes") as fanout:
            fanout.return_value = ([], [])
            self._retry(client, ("disabled", "resource", "gpt-4"))
        (change,) = fanout.call_args.args[2]
        assert (change.data["disabled"], change.data["cascade"]) == (True, False)

    def test_a_level_whose_item_is_gone_is_retried_as_a_delete(self, client):
        with patch("zae_limiter_provisioner.handler._sync_bucket_param_changes") as sync:
            sync.return_value = ([], [])
            self._retry(client, ("sync", "entity", "gone/gpt-4"))
        (change,) = sync.call_args.args[2]
        assert (change.action, change.data) == ("delete", None)

    def test_a_config_written_meanwhile_is_fanned_out_again(self, client):
        """Another apply moved the level after the read this fan-out came from."""
        from zae_limiter_provisioner.applier import apply_changes

        seen = []

        def sync(_table, _ns, changes, **_kw):
            seen.append(changes[0].data["limits"]["rpm"]["capacity"])
            if len(seen) == 1:
                apply_changes(
                    [Change("update", "entity", "u1/gpt-4", {"limits": _limits(20)})],
                    self.TABLE,
                    "ns",
                    client=client,
                )
            return [], []

        with patch("zae_limiter_provisioner.handler._sync_bucket_param_changes", side_effect=sync):
            errors, done = self._retry(client, ("sync", "entity", "u1/gpt-4"))
        assert (errors, seen) == ([], [5, 20])
        done.assert_called_once()

    def test_a_level_that_keeps_moving_is_left_pending(self, client):
        from zae_limiter_provisioner.applier import apply_changes

        def sync(_table, _ns, changes, **_kw):
            capacity = changes[0].data["limits"]["rpm"]["capacity"] + 1
            apply_changes(
                [Change("update", "entity", "u1/gpt-4", {"limits": _limits(capacity)})],
                self.TABLE,
                "ns",
                client=client,
            )
            return [], []

        with patch("zae_limiter_provisioner.handler._sync_bucket_param_changes", side_effect=sync):
            errors, done = self._retry(client, ("sync", "entity", "u1/gpt-4"))
        assert errors == []
        done.assert_not_called()

    def test_a_failure_keeps_the_item_and_is_reported(self, client):
        with patch(
            "zae_limiter_provisioner.handler._fanout_cascade_changes",
            return_value=(["cascade fan-out entity u1/gpt-4: boom"], [("entity", "u1/gpt-4")]),
        ):
            errors, done = self._retry(client, ("cascade", "entity", "u1/gpt-4"))
        assert errors == ["cascade fan-out entity u1/gpt-4: boom"]
        done.assert_not_called()

    def test_an_unreadable_level_keeps_the_item_and_is_reported(self, client):
        from zae_limiter_provisioner.handler import _retry_from_table

        failing = MagicMock()
        failing.get_item.side_effect = RuntimeError("throttled")
        done = MagicMock()
        errors = _retry_from_table(failing, self.TABLE, "ns", ("sync", "entity", "u1/x"), done)
        assert errors == ["retry sync entity u1/x: throttled (retried on the next apply)"]
        done.assert_not_called()


def _limits(capacity: int) -> dict:
    return {"rpm": {"capacity": capacity, "refill_amount": capacity, "refill_period": 60}}


class TestChangeOnlyApplyStaysSafe:
    """The #739 review: a change-only apply must not lose a fan-out it owes."""

    TABLE = "prov-change-only-safety"

    @pytest.fixture
    def counting(self, mock_dynamodb):
        import boto3

        from zae_limiter.sync_repository import SyncRepository

        setup = SyncRepository(name=self.TABLE, region="us-east-1", _skip_deprecation_warning=True)
        setup.create_table()
        setup.close()
        client = _CountingClient(boto3.client("dynamodb", region_name="us-east-1"))
        from zae_limiter_provisioner import pending

        real_now = pending.now_ms
        self._offset_ms = 0
        with (
            patch("boto3.client", return_value=client),
            patch.object(pending, "now_ms", side_effect=lambda: real_now() + self._offset_ms),
        ):
            yield client

    def _later(self):
        """Move the lease clock past every lease: an interrupted apply is gone."""
        from zae_limiter_provisioner.pending import LEASE_MARGIN_MS, MAX_RUN_MS

        self._offset_ms += MAX_RUN_MS + 2 * LEASE_MARGIN_MS

    def _apply(self, manifest, action="apply", **extra):
        return on_event(
            {
                "action": action,
                "table_name": self.TABLE,
                "namespace_id": "ns123",
                "manifest": manifest,
                **extra,
            },
            MagicMock(),
        )

    @staticmethod
    def _manifest(capacity=10, *, disabled=None, resource_disabled=None, entities=("user-0",)):
        entry: dict = {"limits": {"rpm": {"capacity": capacity}}}
        if disabled is not None:
            entry["disabled"] = disabled
        resource: dict = {"limits": {"rpm": {"capacity": 1000}}}
        if resource_disabled is not None:
            resource["disabled"] = resource_disabled
        return {
            "namespace": "test-ns",
            "resources": {"gpt-4": resource},
            "entities": {e: {"resources": {"gpt-4": entry}} for e in entities},
        }

    def _put_bucket(self, client, entity_id, *, disabled=False, capacity=10):
        from zae_limiter.schema import gsi2_pk_resource, gsi2_sk_bucket, gsi3_pk_entity

        item = {
            "PK": {"S": f"ns123/BUCKET#{entity_id}#gpt-4#0"},
            "SK": {"S": "#STATE"},
            "GSI2PK": {"S": gsi2_pk_resource("ns123", "gpt-4")},
            "GSI2SK": {"S": gsi2_sk_bucket(entity_id, 0)},
            "GSI3PK": {"S": gsi3_pk_entity("ns123", entity_id)},
            "GSI3SK": {"S": "BUCKET#gpt-4#0"},
            "b_rpm_cp": {"N": str(capacity * 1000)},
            "b_rpm_ra": {"N": str(capacity * 1000)},
            "b_rpm_rp": {"N": "60"},
            "b_rpm_tk": {"N": str(capacity * 1000)},
            "rf": {"N": "1"},
        }
        if disabled:
            item["disabled"] = {"BOOL": True}
        client.put_item(TableName=self.TABLE, Item=item)

    def _bucket(self, client, entity_id):
        return client.get_item(
            TableName=self.TABLE,
            Key={"PK": {"S": f"ns123/BUCKET#{entity_id}#gpt-4#0"}, "SK": {"S": "#STATE"}},
        )["Item"]

    def _record(self, client):
        from zae_limiter_provisioner.handler import _read_provisioner_state

        return _read_provisioner_state(self.TABLE, "ns123")

    def test_an_apply_killed_before_its_fanouts_is_healed_by_the_next(self, counting):
        """The reproduced case: the config lands, the Lambda dies, nothing fans out."""
        self._apply(self._manifest())
        self._put_bucket(counting, "user-0")

        with patch(
            "zae_limiter_provisioner.handler._fanout_disabled_changes",
            side_effect=TimeoutError("Lambda timed out"),
        ):
            with pytest.raises(TimeoutError):
                self._apply(self._manifest(disabled=True))
        assert "disabled" not in self._bucket(counting, "user-0")
        assert ("disabled", "entity", "user-0/gpt-4") in self._owed(counting)
        self._later()

        result = self._apply(self._manifest(disabled=True))

        assert result["changes"] == []  # the config already matches
        assert self._bucket(counting, "user-0")["disabled"] == {"BOOL": True}
        assert self._pending(counting) == []

    def test_a_killed_limit_change_is_still_synced(self, counting):
        """Nothing re-asserts a limit, so only the written-ahead record heals it."""
        self._apply(self._manifest(capacity=10))
        self._put_bucket(counting, "user-0", capacity=10)

        with patch(
            "zae_limiter_provisioner.handler._sync_bucket_param_changes",
            side_effect=_Killed(),
        ):
            with pytest.raises(_Killed):
                self._apply(self._manifest(capacity=20))
        assert self._bucket(counting, "user-0")["b_rpm_cp"] == {"N": "10000"}
        self._later()

        self._apply(self._manifest(capacity=20))

        assert self._bucket(counting, "user-0")["b_rpm_cp"] == {"N": "20000"}
        assert self._pending(counting) == []

    def test_a_killed_cascade_change_is_still_restamped(self, counting):
        manifest = self._manifest()
        self._apply(manifest)
        manifest["entities"]["user-0"]["resources"]["gpt-4"]["cascade"] = True

        with (
            patch("zae_limiter_provisioner.handler.require_cascade_policy_readers"),
            patch(
                "zae_limiter_provisioner.handler._fanout_cascade_changes",
                side_effect=_Killed(),
            ),
        ):
            with pytest.raises(_Killed):
                self._apply(manifest)
        assert ("cascade", "entity", "user-0/gpt-4") in self._owed(counting)
        self._later()

        with (
            patch("zae_limiter_provisioner.handler.require_cascade_policy_readers"),
            patch("zae_limiter_provisioner.handler.fanout_cascade") as fanout,
        ):
            self._apply(manifest)
        assert fanout.call_args.kwargs == {"entity_id": "user-0", "resource": "gpt-4"}
        assert self._pending(counting) == []

    def test_a_bucket_the_disable_fanout_missed_is_stamped_by_a_reapply(self, counting):
        manifest = self._manifest(resource_disabled=True)
        self._apply(manifest)
        # Created by an acquire the fan-out's discovery query did not see.
        self._put_bucket(counting, "user-0")

        result = self._apply(manifest)

        assert result["changes"] == []
        assert self._bucket(counting, "user-0")["disabled"] == {"BOOL": True}

    def test_a_reasserted_resource_rewrites_no_bucket_already_stamped(self, counting):
        manifest = self._manifest(resource_disabled=True, entities=("user-0", "user-1"))
        self._apply(manifest)
        self._put_bucket(counting, "user-0", disabled=True)
        self._put_bucket(counting, "user-1", disabled=True)
        counting.calls.clear()

        self._apply(manifest)

        assert counting.calls.get("update_item", 0) == 0
        # The pending-items query, then the GSI2 discovery (two passes).
        assert counting.calls["query"] == 3

    def test_an_undeclared_disabled_is_not_reasserted(self, counting):
        self._apply(self._manifest())
        counting.calls.clear()
        self._apply(self._manifest())
        assert counting.calls.get("query", 0) == 1  # only the pending-items query

    def test_full_fans_out_every_declared_level(self, counting):
        manifest = self._manifest(entities=("user-0", "user-1", "user-2"))
        self._apply(manifest)

        with patch("zae_limiter_provisioner.handler.sync_bucket_params") as sync:
            result = self._apply(manifest, full=True)

        assert result["unchanged"] == 0
        assert len(result["changes"]) == 4
        assert sorted(c.kwargs["entity_id"] for c in sync.call_args_list) == [
            "user-0",
            "user-1",
            "user-2",
        ]

    def test_the_version_gates_see_unchanged_entries(self, counting):
        manifest = self._manifest()
        self._apply(manifest)

        with (
            patch("zae_limiter_provisioner.handler.require_reset_after_readers") as reset_gate,
            patch("zae_limiter_provisioner.handler.require_cascade_policy_readers") as cascade_gate,
        ):
            result = self._apply(manifest)

        assert result["changes"] == []
        for gate in (reset_gate, cascade_gate):
            targets = {c.target for c in gate.call_args.args[0]}
            assert targets == {"gpt-4", "user-0/gpt-4"}

    def test_a_large_apply_needs_no_flag_and_no_full_retry(self, counting):
        """No 100-level cap: every owed fan-out is its own item, however many."""
        entities = tuple(f"user-{i}" for i in range(150))
        with patch(
            "zae_limiter_provisioner.handler._sync_bucket_param_changes",
            side_effect=_Killed(),
        ):
            with pytest.raises(_Killed):
                self._apply(self._manifest(entities=entities))
        assert len(self._owed(counting)) == len(entities)
        self._later()

        with patch("zae_limiter_provisioner.handler.sync_bucket_params") as sync:
            result = self._apply(self._manifest(entities=entities))

        assert result["changes"] == []  # change-only: no full re-apply
        assert sync.call_count == len(entities)
        assert self._pending(counting) == []

    def test_a_plan_reports_the_fan_outs_an_interrupted_apply_left(self, counting):
        self._apply(self._manifest(capacity=10))
        with patch(
            "zae_limiter_provisioner.handler._sync_bucket_param_changes",
            side_effect=_Killed(),
        ):
            with pytest.raises(_Killed):
                self._apply(self._manifest(capacity=20))
        counting.calls.clear()

        result = self._apply(self._manifest(capacity=20), action="plan")

        assert result["changes"] == []
        assert result["pending"] == 1
        assert result["full"] is False
        # Still read-only: the item stays for the apply that retries it.
        assert counting.calls.get("put_item", 0) == counting.calls.get("update_item", 0) == 0
        assert len(self._pending(counting)) == 1

    def test_a_plan_with_nothing_owed_is_change_only(self, counting):
        manifest = self._manifest()
        self._apply(manifest)

        result = self._apply(manifest, action="plan")

        assert result["full"] is False
        assert result["pending"] == 0
        assert result["changes"] == []

    def test_an_entity_carve_out_is_reasserted_too(self, counting):
        manifest = self._manifest(disabled=False, resource_disabled=True)
        self._apply(manifest)
        # A bucket the earlier fan-out missed, stamped by a racing acquire
        # that still read the resource as disabled.
        self._put_bucket(counting, "user-0", disabled=True)

        self._apply(manifest)

        assert "disabled" not in self._bucket(counting, "user-0")

    def test_a_concurrent_apply_cannot_erase_another_applys_pending_work(self, counting):
        """Apply A is killed after its config writes while apply B is mid-flight.

        B read the pending state before A wrote its own ahead, and finishes
        after A died. Nothing B writes may drop the sync A still owes.
        """
        from zae_limiter_provisioner import handler

        self._apply(self._manifest(capacity=10))
        self._put_bucket(counting, "user-0", capacity=10)
        real_sync = handler._sync_bucket_param_changes

        def b_sync(*args, **kwargs):
            with patch.object(handler, "_sync_bucket_param_changes", side_effect=_Killed()):
                with pytest.raises(_Killed):
                    self._apply(self._manifest(capacity=20))
            return real_sync(*args, **kwargs)

        # B: an unrelated change (a new entity), fanning out while A runs.
        with patch.object(handler, "_sync_bucket_param_changes", side_effect=b_sync):
            self._apply(self._manifest(capacity=10, entities=("user-0", "user-1")))

        # The next apply of A's manifest finds its config already in place.
        self._later()
        result = self._apply(self._manifest(capacity=20, entities=("user-0", "user-1")))

        assert self._bucket(counting, "user-0")["b_rpm_cp"] == {"N": "20000"}
        assert result["errors"] == []

    def test_an_interrupted_apply_resumes_where_it_stopped(self, counting):
        """Each run is killed after syncing 50 levels; three runs cover all 101."""
        from zae_limiter_provisioner import handler

        entities = tuple(f"user-{i}" for i in range(101))
        synced: list[str] = []
        real_sync = handler.sync_bucket_params
        budget = {"left": 0}

        def sync_until_killed(*args, **kwargs):
            if budget["left"] == 0:
                raise _Killed()
            budget["left"] -= 1
            synced.append(kwargs["entity_id"])
            return real_sync(*args, **kwargs)

        with patch.object(handler, "sync_bucket_params", side_effect=sync_until_killed):
            for _attempt in range(3):
                budget["left"] = 50
                self._later()
                try:
                    self._apply(self._manifest(entities=entities))
                except _Killed:
                    continue

        assert len(synced) == len(entities)
        assert set(synced) == set(entities)
        assert self._pending(counting) == []

    def test_an_interrupted_full_apply_resumes_instead_of_restarting(self, counting):
        """Review finding 5: a full apply too long for one run must still converge."""
        from zae_limiter_provisioner import handler

        entities = tuple(f"user-{i}" for i in range(20))
        manifest = self._manifest(entities=entities)
        self._apply(manifest)
        real_sync = handler.sync_bucket_params
        budget = {"left": 10}

        def sync_until_killed(*args, **kwargs):
            if budget["left"] == 0:
                raise _Killed()
            budget["left"] -= 1
            return real_sync(*args, **kwargs)

        with patch.object(handler, "sync_bucket_params", side_effect=sync_until_killed):
            with pytest.raises(_Killed):
                self._apply(manifest, full=True)
        assert len([i for i in self._pending(counting) if i["kind"]["S"] == "sync"]) == 10
        self._later()

        with (
            patch.object(handler, "sync_bucket_params", wraps=real_sync) as sync,
            patch.object(handler, "fanout_entity", wraps=real_fanout_entity) as disabled,
            patch.object(handler, "fanout_cascade", wraps=real_fanout_cascade) as cascade,
        ):
            result = self._apply(manifest, full=True)

        # Only the ten syncs the first run did not reach; its disable and
        # cascade fan-outs had all landed.
        assert sync.call_count == 10
        assert disabled.call_count == cascade.call_count == 0
        assert (result["full"], result["resumed"], result["changes"]) == (True, True, [])
        assert self._pending(counting) == []

    @pytest.mark.parametrize("stuck", ["leased to a live apply", "left by a change-only apply"])
    def test_a_foreign_pending_item_does_not_turn_full_into_change_only(self, counting, stuck):
        """Re-review #3: any pending item made `--full` silently change-only.

        A full apply is the repair for a bucket that drifted while its config
        still matches, so an item it did not write must not stop it.
        """
        from zae_limiter_provisioner.pending import new_token, write_ahead

        self._apply(self._manifest(capacity=10, entities=("user-0", "user-1")))
        self._put_bucket(counting, "user-0", capacity=10)
        counting.update_item(
            TableName=self.TABLE,
            Key={"PK": {"S": "ns123/BUCKET#user-0#gpt-4#0"}, "SK": {"S": "#STATE"}},
            UpdateExpression="SET b_rpm_cp = :c",
            ExpressionAttributeValues={":c": {"N": "999000"}},
        )
        foreign = ("sync", "entity", "user-1/gpt-4")
        lease = None if stuck == "leased to a live apply" else 0
        write_ahead(counting, self.TABLE, "ns123", new_token(), [foreign], lease_until_ms=lease)

        result = self._apply(self._manifest(capacity=10, entities=("user-0", "user-1")), full=True)

        assert (result["full"], result["resumed"]) == (True, False)
        assert len(result["changes"]) == 3
        assert self._bucket(counting, "user-0")["b_rpm_cp"] == {"N": "10000"}
        owed = self._owed(counting)
        # A live apply's item stays its own; a claimable one was retried.
        assert owed == ({foreign} if lease is None else set())

    def test_a_full_apply_with_nothing_pending_fans_out_every_level(self, counting):
        from zae_limiter_provisioner import handler

        entities = ("user-0", "user-1", "user-2")
        manifest = self._manifest(entities=entities)
        self._apply(manifest)

        with (
            patch.object(handler, "sync_bucket_params", wraps=handler.sync_bucket_params) as sync,
            patch.object(handler, "fanout_cascade", wraps=real_fanout_cascade) as cascade,
        ):
            result = self._apply(manifest, full=True)

        assert sync.call_count == len(entities)
        assert cascade.call_count == len(entities) + 1  # the resource too
        assert (result["full"], result["resumed"], len(result["changes"])) == (True, False, 4)
        assert self._pending(counting) == []

    def _throttle_config_puts(self, counting):
        from botocore.exceptions import ClientError

        real_put = counting._client.put_item

        def throttled(**kwargs):
            if kwargs["Item"]["SK"]["S"].startswith("#CONFIG"):
                raise ClientError(
                    {"Error": {"Code": "ProvisionedThroughputExceededException"}}, "PutItem"
                )
            return real_put(**kwargs)

        counting.put_item = throttled

    def test_a_failed_config_write_fans_out_nothing_it_did_not_write(self, counting):
        """Re-review #4: a predicted fan-out ran even when its config write failed.

        Every owed fan-out reads the live config (re-review #1), so a write
        that never landed leaves the buckets as the table says they should be.
        """
        self._apply(self._manifest(capacity=10))
        self._put_bucket(counting, "user-0", capacity=10)
        self._throttle_config_puts(counting)
        try:
            result = self._apply(self._manifest(capacity=20, resource_disabled=True))
        finally:
            del counting.put_item

        assert any("ProvisionedThroughputExceeded" in e for e in result["errors"])
        bucket = self._bucket(counting, "user-0")
        assert "disabled" not in bucket
        assert bucket["b_rpm_cp"] == {"N": "10000"}

    def test_an_applys_own_sync_fans_out_the_live_config(self, counting):
        """Re-review #1: A writes 10, B writes 20 and syncs, then A's late sync runs.

        A's own fan-out came from A's manifest and stamped 10 over B's 20,
        leaving config 20, buckets 10, nothing pending, and nothing that a
        re-apply would see as a change.
        """
        from zae_limiter_provisioner import handler

        self._apply(self._manifest(capacity=5))
        self._put_bucket(counting, "user-0", capacity=5)
        real_sync = handler._sync_bucket_param_changes
        state = {"b_ran": False}

        def a_sync(*args, **kwargs):
            if not state["b_ran"]:
                state["b_ran"] = True
                self._apply(self._manifest(capacity=20))  # B, start to finish
            return real_sync(*args, **kwargs)

        with patch.object(handler, "_sync_bucket_param_changes", side_effect=a_sync):
            self._apply(self._manifest(capacity=10))  # A

        config = counting.get_item(
            TableName=self.TABLE,
            Key={"PK": {"S": pk_entity("ns123", "user-0")}, "SK": {"S": sk_config("gpt-4")}},
        )["Item"]
        assert config["l_rpm_cp"] == {"N": "20"}
        assert self._bucket(counting, "user-0")["b_rpm_cp"] == {"N": "20000"}
        assert self._pending(counting) == []

    def test_a_concurrent_apply_cannot_release_work_before_its_config_lands(self, counting):
        """Re-review #2: B claims and releases A's item before A's config write.

        B's retry read the old config twice, the reads agreed, and B released
        the item; A then wrote its config and died. The fan-out was lost and
        the bucket stayed at 5 forever.
        """
        from zae_limiter_provisioner import handler

        self._apply(self._manifest(capacity=5))
        self._put_bucket(counting, "user-0", capacity=5)
        real_apply = handler.apply_changes
        real_sync = handler._sync_bucket_param_changes
        state = {"b_ran": False, "in_b": False}

        def a_writes(*args, **kwargs):
            if not state["b_ran"]:
                state["b_ran"] = state["in_b"] = True
                try:
                    self._apply(self._manifest(capacity=5))  # B, while A is between steps
                finally:
                    state["in_b"] = False
            return real_apply(*args, **kwargs)

        def a_dies(*args, **kwargs):
            if state["in_b"]:
                return real_sync(*args, **kwargs)
            raise _Killed()

        with (
            patch.object(handler, "apply_changes", side_effect=a_writes),
            patch.object(handler, "_sync_bucket_param_changes", side_effect=a_dies),
        ):
            with pytest.raises(_Killed):
                self._apply(self._manifest(capacity=10))  # A, killed after its write

        assert ("sync", "entity", "user-0/gpt-4") in self._owed(counting)
        self._later()
        self._apply(self._manifest(capacity=10))

        assert self._bucket(counting, "user-0")["b_rpm_cp"] == {"N": "10000"}
        assert self._pending(counting) == []

    def test_a_stale_reapply_leaves_a_live_applys_sync_to_it(self, counting):
        """Review finding 1, then re-review #2: a stale no-op re-apply and B's sync.

        Apply B is moving user-0 from 10 to 20 and has written its sync ahead.
        The stale apply, running meanwhile, must not take B's item while B's
        lease runs, so it can neither sync the bucket back to 10 nor release
        what B still owes. B then finishes and removes its own item.
        """
        from zae_limiter_provisioner import handler
        from zae_limiter_provisioner.applier import apply_changes
        from zae_limiter_provisioner.manifest import LimitsManifest
        from zae_limiter_provisioner.pending import new_token, release, write_ahead

        table = self.TABLE
        self._apply(self._manifest(capacity=10))
        self._put_bucket(counting, "user-0", capacity=10)
        key = ("sync", "entity", "user-0/gpt-4")
        tok_b = new_token()
        write_ahead(counting, table, "ns123", tok_b, [key])

        self._apply(self._manifest(capacity=10))  # the stale apply
        assert [i["token"]["S"] for i in self._pending(counting)] == [tok_b]

        # B writes its config and syncs, then removes its own item.
        manifest = LimitsManifest.from_dict(self._manifest(capacity=20))
        new = manifest.entities["user-0"].resources["gpt-4"].to_dict()
        res = apply_changes([Change("update", "entity", "user-0/gpt-4", new)], table, "ns123")
        handler._sync_bucket_param_changes(
            table,
            "ns123",
            res.limits_changed,
            on_done=lambda *k: release(counting, table, "ns123", tok_b, k),
        )

        assert self._pending(counting) == []
        assert self._bucket(counting, "user-0")["b_rpm_cp"] == {"N": "20000"}

    def test_a_delete_runs_only_the_fan_outs_its_level_needed(self, counting):
        """A deleted entity level that set no `disabled` or `cascade` needs only the sync."""
        self._apply(self._manifest(entities=("user-0", "user-1")))
        self._put_bucket(counting, "user-1")
        counting.calls.clear()

        with (
            patch(
                "zae_limiter_provisioner.handler.fanout_entity", wraps=real_fanout_entity
            ) as disabled,
            patch(
                "zae_limiter_provisioner.handler.fanout_cascade", wraps=real_fanout_cascade
            ) as cascade,
        ):
            self._apply(self._manifest(entities=("user-0",)))

        disabled.assert_not_called()
        cascade.assert_not_called()
        # The pending-items query, then the sync's bucket discovery (two passes).
        assert counting.calls["query"] == 3
        # Reconciled to the resource default.
        assert self._bucket(counting, "user-1")["b_rpm_cp"] == {"N": "1000000"}
        assert self._pending(counting) == []

    def test_a_resent_delete_still_runs_what_the_pre_read_predicted(self, counting):
        """The re-send's `ALL_OLD` is empty; the pre-read saw the deleted level."""
        manifest = self._manifest(disabled=True, entities=("user-0", "user-1"))
        self._apply(manifest)
        self._put_bucket(counting, "user-1", disabled=True)
        real_delete = counting._client.delete_item

        def resend(**kwargs):
            if kwargs["Key"]["SK"]["S"].startswith("#CONFIG"):
                real_delete(**kwargs)  # landed; its response was lost
            return real_delete(**kwargs)

        counting.delete_item = resend
        try:
            with patch(
                "zae_limiter_provisioner.handler.fanout_cascade", wraps=real_fanout_cascade
            ) as cascade:
                self._apply(self._manifest(disabled=True, entities=("user-0",)))
        finally:
            del counting.delete_item

        bucket = self._bucket(counting, "user-1")
        assert "disabled" not in bucket  # the level that disabled it is gone
        assert bucket["b_rpm_cp"] == {"N": "1000000"}
        cascade.assert_not_called()  # the deleted level set no cascade policy
        assert self._pending(counting) == []

    def test_a_resent_config_write_still_fans_out(self, counting):
        """Review finding 2: botocore re-sends a write whose first try landed.

        The re-send's ``ALL_OLD`` already equals the new item, so the write's
        own image shows no change. The strongly consistent pre-read saw the
        old item and predicted the sync; that prediction must stand.
        """
        self._apply(self._manifest(capacity=10))
        self._put_bucket(counting, "user-0", capacity=10)
        real_put = counting._client.put_item

        def resend(**kwargs):
            if kwargs["Item"]["SK"]["S"].startswith("#CONFIG"):
                real_put(**kwargs)  # landed; its response was lost
            return real_put(**kwargs)

        counting.put_item = resend
        try:
            self._apply(self._manifest(capacity=20))
        finally:
            del counting.put_item

        assert self._bucket(counting, "user-0")["b_rpm_cp"] == {"N": "20000"}
        assert self._pending(counting) == []

    def test_a_retried_level_the_manifest_dropped_keeps_its_live_disabled(self, counting):
        """Review finding 4: a retry is not a synthetic delete stamping `disabled=False`.

        An apply disables a resource and dies after its config write. The
        next apply's manifest does not declare the resource (and, the dead
        apply having recorded nothing, does not delete it either): the config
        still says disabled, so its buckets must be stamped disabled.
        """
        disabled = {
            "namespace": "test-ns",
            "resources": {"gpt-4": {"disabled": True, "limits": {"rpm": {"capacity": 1000}}}},
        }
        self._put_bucket(counting, "user-0")
        with patch(
            "zae_limiter_provisioner.handler._fanout_disabled_changes", side_effect=_Killed()
        ):
            with pytest.raises(_Killed):
                self._apply(disabled)
        assert ("disabled", "resource", "gpt-4") in self._owed(counting)
        self._later()

        self._apply(
            {
                "namespace": "test-ns",
                "entities": {
                    "user-0": {"resources": {"gpt-4": {"limits": {"rpm": {"capacity": 10}}}}}
                },
            }
        )

        assert self._bucket(counting, "user-0")["disabled"] == {"BOOL": True}
        assert self._pending(counting) == []

    def _owed(self, client):
        return {(i["kind"]["S"], i["level"]["S"], i["target"]["S"]) for i in self._pending(client)}

    def _pending(self, client):
        return client.query(
            TableName=self.TABLE,
            KeyConditionExpression="PK = :pk AND begins_with(SK, :sk)",
            ExpressionAttributeValues={
                ":pk": {"S": pk_system("ns123")},
                ":sk": {"S": "#PROVISIONER#PENDING#"},
            },
        )["Items"]

    def _cfn(self, request_type="Update", **extra):
        return on_event(
            {
                "RequestType": request_type,
                "ResourceProperties": {
                    "ServiceToken": "arn:aws:lambda:us-east-1:123:function:test",
                    "TableName": self.TABLE,
                    "Namespace": "test-ns",
                    "NamespaceId": "ns123",
                    "Resources": {"gpt-4": {"Limits": {"rpm": {"Capacity": "1000"}}}},
                    "Entities": {
                        "user-0": {"Resources": {"gpt-4": {"Limits": {"rpm": {"Capacity": "10"}}}}}
                    },
                    **extra,
                },
            },
            MagicMock(),
        )

    def _drift(self, client):
        """A bucket whose params drifted out of band; its config item still matches."""
        self._cfn(request_type="Create")
        self._put_bucket(client, "user-0", capacity=10)
        client.update_item(
            TableName=self.TABLE,
            Key={"PK": {"S": "ns123/BUCKET#user-0#gpt-4#0"}, "SK": {"S": "#STATE"}},
            UpdateExpression="SET b_rpm_cp = :c",
            ExpressionAttributeValues={":c": {"N": "999000"}},
        )

    def test_a_change_only_cfn_update_leaves_a_drifted_bucket(self, counting):
        """The residual `FullApply` exists for: nothing changed, so nothing syncs."""
        self._drift(counting)

        result = self._cfn()

        assert result["changes"] == []
        assert self._bucket(counting, "user-0")["b_rpm_cp"] == {"N": "999000"}

    def test_a_cfn_full_apply_repairs_a_drifted_bucket(self, counting):
        self._drift(counting)

        result = self._cfn(FullApply="true")

        assert result["unchanged"] == 0
        assert len(result["changes"]) == 2
        assert self._bucket(counting, "user-0")["b_rpm_cp"] == {"N": "10000"}

    def test_cfn_full_apply_false_is_change_only(self, counting):
        self._drift(counting)

        result = self._cfn(FullApply="false")

        assert result["changes"] == []
        assert self._bucket(counting, "user-0")["b_rpm_cp"] == {"N": "999000"}

    def test_cfn_full_apply_must_be_a_boolean(self, counting):
        with pytest.raises(ValueError, match="FullApply must be true or false"):
            self._cfn(request_type="Create", FullApply="yes")


class TestChangeOnlyApplyNonEnforcing:
    """Soft limits and `disabled: bypass` (ADR-151) under the change-only apply."""

    TABLE = "prov-change-only-non-enforcing"

    @pytest.fixture
    def counting(self, mock_dynamodb):
        import boto3

        from zae_limiter.sync_repository import SyncRepository

        setup = SyncRepository(name=self.TABLE, region="us-east-1", _skip_deprecation_warning=True)
        setup.create_table()
        setup.close()
        client = _CountingClient(boto3.client("dynamodb", region_name="us-east-1"))
        with (
            patch("boto3.client", return_value=client),
            # The 0.17.0 gate needs a version record; its own tests cover it.
            patch("zae_limiter_provisioner.handler.require_non_enforcing_readers"),
        ):
            yield client

    def _apply(self, manifest, action="apply"):
        return on_event(
            {
                "action": action,
                "table_name": self.TABLE,
                "namespace_id": "ns123",
                "manifest": manifest,
            },
            MagicMock(),
        )

    @staticmethod
    def _manifest(*, resource_soft=False, entity_soft=False, resource_disabled=None):
        resource: dict = {"limits": {"rpm": {"capacity": 1000, "soft": resource_soft}}}
        if resource_disabled is not None:
            resource["disabled"] = resource_disabled
        entity = {"limits": {"rpm": {"capacity": 10, "soft": entity_soft}}}
        return {
            "namespace": "test-ns",
            "resources": {"gpt-4": resource},
            "entities": {"user-0": {"resources": {"gpt-4": entity}}},
        }

    def _put_bucket(self, client, entity_id, **extra):
        from zae_limiter.schema import (
            gsi2_pk_resource,
            gsi2_sk_bucket,
            gsi3_pk_entity,
            gsi4_sk_bucket,
        )

        item = {
            "PK": {"S": f"ns123/BUCKET#{entity_id}#gpt-4#0"},
            "SK": {"S": "#STATE"},
            "GSI2PK": {"S": gsi2_pk_resource("ns123", "gpt-4")},
            "GSI2SK": {"S": gsi2_sk_bucket(entity_id, 0)},
            "GSI3PK": {"S": gsi3_pk_entity("ns123", entity_id)},
            "GSI3SK": {"S": "BUCKET#gpt-4#0"},
            # As the client writes every bucket: a system-level fan-out finds
            # buckets through GSI4.
            "GSI4PK": {"S": "ns123"},
            "GSI4SK": {"S": gsi4_sk_bucket(entity_id, "gpt-4", 0)},
            "b_rpm_cp": {"N": "10000"},
            "b_rpm_ra": {"N": "10000"},
            "b_rpm_rp": {"N": "60"},
            "b_rpm_tk": {"N": "10000"},
            "rf": {"N": "1"},
            **extra,
        }
        client.put_item(TableName=self.TABLE, Item=item)

    def _bucket(self, client, entity_id):
        return client.get_item(
            TableName=self.TABLE,
            Key={"PK": {"S": f"ns123/BUCKET#{entity_id}#gpt-4#0"}, "SK": {"S": "#STATE"}},
        )["Item"]

    def test_a_resource_change_of_only_soft_is_applied_and_fanned_out(self, counting):
        self._apply(self._manifest())

        with patch("zae_limiter_provisioner.handler.fanout_soft") as fanout:
            result = self._apply(self._manifest(resource_soft=True))

        assert result["changes"] == [{"action": "update", "level": "resource", "target": "gpt-4"}]
        fanout.assert_called_once()
        assert fanout.call_args.args[3:] == ("gpt-4", {"rpm"})

    def test_an_entity_change_of_only_soft_restamps_its_buckets(self, counting):
        self._apply(self._manifest())
        self._put_bucket(counting, "user-0")

        result = self._apply(self._manifest(entity_soft=True))

        assert result["changes"] == [
            {"action": "update", "level": "entity", "target": "user-0/gpt-4"}
        ]
        assert "N" in self._bucket(counting, "user-0")["b_rpm_soft"]

    def test_an_unchanged_soft_level_fans_out_nothing(self, counting):
        self._apply(self._manifest(resource_soft=True, entity_soft=True))
        counting.calls.clear()

        with patch("zae_limiter_provisioner.handler.fanout_soft") as fanout:
            result = self._apply(self._manifest(resource_soft=True, entity_soft=True))

        assert result["changes"] == []
        fanout.assert_not_called()
        assert counting.calls.get("query", 0) == 1  # only the pending-items query

    def test_a_change_to_bypass_alone_restamps_the_buckets(self, counting):
        self._apply(self._manifest(resource_disabled=True))
        self._put_bucket(counting, "user-0", disabled={"BOOL": True})

        result = self._apply(self._manifest(resource_disabled="bypass"))

        assert result["changes"] == [{"action": "update", "level": "resource", "target": "gpt-4"}]
        bucket = self._bucket(counting, "user-0")
        assert "disabled" not in bucket
        assert "N" in bucket["bypass"]

    def test_a_reasserted_bypass_stamps_only_a_missed_bucket(self, counting):
        manifest = self._manifest(resource_disabled="bypass")
        self._apply(manifest)
        self._put_bucket(counting, "user-0", bypass={"N": "1"})  # stamped, even if expired
        self._put_bucket(counting, "user-1")  # missed by the fan-out's discovery
        counting.calls.clear()

        result = self._apply(manifest)

        assert result["changes"] == []
        assert self._bucket(counting, "user-0")["bypass"] == {"N": "1"}
        assert "N" in self._bucket(counting, "user-1")["bypass"]
        assert counting.calls.get("update_item", 0) == 1

    def _resend_config_puts(self, counting):
        real_put = counting._client.put_item

        def resend(**kwargs):
            if kwargs["Item"]["SK"]["S"].startswith("#CONFIG"):
                real_put(**kwargs)  # landed; its response was lost
            return real_put(**kwargs)

        counting.put_item = resend

    @pytest.mark.parametrize("level", ["resource", "system"])
    def test_a_resent_soft_write_still_fans_out(self, counting, level):
        """Re-review #5: soft-ness came only from `ALL_OLD`, so a re-send lost it.

        A bucket on the changed level's defaults (no entity config of its
        own) must be stamped soft.
        """
        manifest = self._manifest()
        if level == "system":
            manifest["system"] = {"limits": {"rpm": {"capacity": 1000}}}
            del manifest["resources"]
        self._apply(manifest)
        self._put_bucket(counting, "user-9")
        if level == "system":
            manifest["system"]["limits"]["rpm"]["soft"] = True
        else:
            manifest["resources"]["gpt-4"]["limits"]["rpm"]["soft"] = True

        self._resend_config_puts(counting)
        try:
            self._apply(manifest)
        finally:
            del counting.put_item

        assert "N" in self._bucket(counting, "user-9")["b_rpm_soft"]
        assert self._pending(counting) == []

    def _pending(self, client):
        return client.query(
            TableName=self.TABLE,
            KeyConditionExpression="PK = :pk AND begins_with(SK, :sk)",
            ExpressionAttributeValues={
                ":pk": {"S": pk_system("ns123")},
                ":sk": {"S": "#PROVISIONER#PENDING#"},
            },
        )["Items"]

    def test_the_non_enforcing_gate_sees_unchanged_entries(self, counting):
        manifest = self._manifest(resource_soft=True)
        self._apply(manifest)

        with patch("zae_limiter_provisioner.handler.require_non_enforcing_readers") as gate:
            result = self._apply(manifest)

        assert result["changes"] == []
        assert {c.target for c in gate.call_args.args[0]} == {"gpt-4", "user-0/gpt-4"}
