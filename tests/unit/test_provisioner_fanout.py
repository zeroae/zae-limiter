"""Tests for the provisioner's sync boto3 bucket fan-out (ADR-125).

Mirrors the mocking conventions in test_provisioner_handler.py / test_applier.py:
a MagicMock boto3 DynamoDB client, with `client.exceptions.*` populated with real
exception classes so `except client.exceptions.X` matches in tests the same way it
does against a real boto3 client.
"""

from unittest.mock import MagicMock

from zae_limiter.schema import DEFAULT_RESOURCE, pk_entity, pk_resource, sk_config
from zae_limiter_provisioner.fanout import (
    fanout_entity,
    fanout_resource,
    resolve_disabled,
    stamp_bucket,
)

# Use type() to create a class with the AWS name (avoids N818 lint rule),
# matching the convention in test_limiter.py's TestLeaseRetryPath.
ConditionalCheckFailedException = type("ConditionalCheckFailedException", (Exception,), {})


def _make_client() -> MagicMock:
    client = MagicMock()
    client.exceptions.ConditionalCheckFailedException = ConditionalCheckFailedException
    return client


def _item_router(items: dict[tuple[str, str], bool]):
    """Build a `client.get_item` side_effect that looks up (PK, SK) tuples.

    A key present in `items` returns a DynamoDB item with `disabled` set to
    that bool; any other key resolves to "no Item" (mirrors an absent
    `disabled` attribute, i.e. resolve_disabled continuing to the next level
    in its walk).
    """

    def _get_item(*args, **kwargs):
        raw_key = kwargs["Key"]
        key = (raw_key["PK"]["S"], raw_key["SK"]["S"])
        if key in items:
            return {"Item": {"disabled": {"BOOL": items[key]}}}
        return {}

    return _get_item


def _resource_disabled(value: bool, resource: str = "gpt-4", namespace_id: str = "ns123"):
    """A `get_item` side_effect where only the resource level sets `disabled`.

    fanout_resource re-resolves every discovered bucket's entity, so a test
    that expects stamping has to make the resource level agree with the
    directive being applied.
    """
    return _item_router({(pk_resource(namespace_id, resource), sk_config()): value})


class TestStampBucket:
    """Tests for stamp_bucket's SET/REMOVE branching and error swallowing."""

    def test_disabled_true_sets_bool_attribute(self):
        client = _make_client()
        stamp_bucket(client, "test-table", "ns123/BUCKET#user-1#gpt-4#0", disabled=True)

        client.update_item.assert_called_once()
        kwargs = client.update_item.call_args.kwargs
        assert kwargs["TableName"] == "test-table"
        assert kwargs["Key"] == {
            "PK": {"S": "ns123/BUCKET#user-1#gpt-4#0"},
            "SK": {"S": "#STATE"},
        }
        assert kwargs["UpdateExpression"] == "SET #disabled = :true"
        assert kwargs["ExpressionAttributeValues"] == {":true": {"BOOL": True}}
        assert kwargs["ConditionExpression"] == "attribute_exists(PK)"
        assert kwargs["ExpressionAttributeNames"] == {"#disabled": "disabled"}

    def test_disabled_false_removes_attribute(self):
        client = _make_client()
        stamp_bucket(client, "test-table", "ns123/BUCKET#user-1#gpt-4#0", disabled=False)

        kwargs = client.update_item.call_args.kwargs
        assert kwargs["UpdateExpression"] == "REMOVE #disabled"
        assert "ExpressionAttributeValues" not in kwargs

    def test_conditional_check_failure_is_swallowed(self):
        """A bucket that vanished (TTL/delete) between discovery and stamp is not an error."""
        client = _make_client()
        client.update_item.side_effect = ConditionalCheckFailedException()

        # Must not raise.
        stamp_bucket(client, "test-table", "ns123/BUCKET#gone#gpt-4#0", disabled=True)

        client.update_item.assert_called_once()


class TestFanoutResource:
    """Tests for fanout_resource: GSI2 query shape and pagination."""

    def test_queries_gsi2_with_resource_pk(self):
        client = _make_client()
        client.query.return_value = {"Items": []}

        count = fanout_resource(client, "test-table", "ns123", "gpt-4", disabled=True)

        assert count == 0
        # Two discovery passes (Finding 2) even when nothing is found.
        assert client.query.call_count == 2
        kwargs = client.query.call_args.kwargs
        assert kwargs["TableName"] == "test-table"
        assert kwargs["IndexName"] == "GSI2"
        assert kwargs["KeyConditionExpression"] == "GSI2PK = :pk AND begins_with(GSI2SK, :sk)"
        assert kwargs["ExpressionAttributeValues"] == {
            ":pk": {"S": "ns123/RESOURCE#gpt-4"},
            ":sk": {"S": "BUCKET#"},
        }
        assert "ExclusiveStartKey" not in kwargs

    def test_stamps_every_discovered_bucket(self):
        client = _make_client()
        client.query.return_value = {
            "Items": [
                {"PK": {"S": "ns123/BUCKET#user-1#gpt-4#0"}},
                {"PK": {"S": "ns123/BUCKET#user-2#gpt-4#0"}},
            ]
        }
        client.get_item.side_effect = _resource_disabled(True)

        count = fanout_resource(client, "test-table", "ns123", "gpt-4", disabled=True)

        assert count == 2
        assert client.update_item.call_count == 2
        stamped_pks = {c.kwargs["Key"]["PK"]["S"] for c in client.update_item.call_args_list}
        assert stamped_pks == {"ns123/BUCKET#user-1#gpt-4#0", "ns123/BUCKET#user-2#gpt-4#0"}

    def test_paginates_via_last_evaluated_key(self):
        """A second query page is fetched and its ExclusiveStartKey comes from the first."""
        client = _make_client()
        page1_key = {"PK": {"S": "ns123/BUCKET#user-1#gpt-4#0"}, "SK": {"S": "#STATE"}}
        client.query.side_effect = [
            {
                "Items": [{"PK": {"S": "ns123/BUCKET#user-1#gpt-4#0"}}],
                "LastEvaluatedKey": page1_key,
            },
            {"Items": [{"PK": {"S": "ns123/BUCKET#user-2#gpt-4#0"}}]},
            # Second pass (Finding 2): nothing new discovered.
            {"Items": []},
        ]
        client.get_item.side_effect = _resource_disabled(True)

        count = fanout_resource(client, "test-table", "ns123", "gpt-4", disabled=True)

        assert count == 2
        assert client.query.call_count == 3
        first_call_kwargs = client.query.call_args_list[0].kwargs
        second_call_kwargs = client.query.call_args_list[1].kwargs
        third_call_kwargs = client.query.call_args_list[2].kwargs
        assert "ExclusiveStartKey" not in first_call_kwargs
        assert second_call_kwargs["ExclusiveStartKey"] == page1_key
        # Second pass starts a fresh page, not continuing off page1_key.
        assert "ExclusiveStartKey" not in third_call_kwargs

    def test_deduplicates_pk_seen_across_pages(self):
        """The same PK returned on two pages within one pass is stamped only once."""
        client = _make_client()
        dup_key = {"PK": {"S": "ns123/BUCKET#user-1#gpt-4#0"}, "SK": {"S": "#STATE"}}
        client.query.side_effect = [
            {
                "Items": [{"PK": {"S": "ns123/BUCKET#user-1#gpt-4#0"}}],
                "LastEvaluatedKey": dup_key,
            },
            {"Items": [{"PK": {"S": "ns123/BUCKET#user-1#gpt-4#0"}}]},
            # Second pass rediscovers the same bucket again.
            {"Items": [{"PK": {"S": "ns123/BUCKET#user-1#gpt-4#0"}}]},
        ]
        client.get_item.side_effect = _resource_disabled(True)

        count = fanout_resource(client, "test-table", "ns123", "gpt-4", disabled=True)

        assert count == 1
        assert client.update_item.call_count == 1

    def test_second_pass_stamps_bucket_created_after_first_pass_query(self):
        """A bucket that appears only on the second pass is still stamped (ADR-125).

        Mirrors the race `Repository._fanout_resource`'s two passes guard
        against: an `acquire()` already in flight when the first pass's query
        ran can create a bucket the first pass never saw. The second pass's
        fresh query catches it.
        """
        client = _make_client()
        client.query.side_effect = [
            # Pass 1: only the pre-existing bucket is visible.
            {"Items": [{"PK": {"S": "ns123/BUCKET#user-1#gpt-4#0"}}]},
            # Pass 2: a second bucket, created mid-fanout, is now visible too.
            {
                "Items": [
                    {"PK": {"S": "ns123/BUCKET#user-1#gpt-4#0"}},
                    {"PK": {"S": "ns123/BUCKET#user-2#gpt-4#0"}},
                ]
            },
        ]
        client.get_item.side_effect = _resource_disabled(True)

        count = fanout_resource(client, "test-table", "ns123", "gpt-4", disabled=True)

        assert count == 2
        # user-1 stamped once (pass 1), user-2 stamped once (pass 2) -> 2 total.
        assert client.update_item.call_count == 2
        stamped_pks = {c.kwargs["Key"]["PK"]["S"] for c in client.update_item.call_args_list}
        assert stamped_pks == {"ns123/BUCKET#user-1#gpt-4#0", "ns123/BUCKET#user-2#gpt-4#0"}


class TestFanoutEntity:
    """Tests for fanout_entity: GSI3 query shape, resource-scoping, pagination."""

    def test_queries_gsi3_with_entity_pk_unscoped(self):
        client = _make_client()
        client.query.return_value = {"Items": []}

        count = fanout_entity(client, "test-table", "ns123", "vip-1", resource=None, disabled=True)

        assert count == 0
        kwargs = client.query.call_args.kwargs
        assert kwargs["IndexName"] == "GSI3"
        assert kwargs["KeyConditionExpression"] == "GSI3PK = :pk AND begins_with(GSI3SK, :sk)"
        assert kwargs["ExpressionAttributeValues"] == {
            ":pk": {"S": "ns123/ENTITY#vip-1"},
            ":sk": {"S": "BUCKET#"},
        }

    def test_scoped_to_resource_uses_resource_prefix(self):
        client = _make_client()
        client.query.return_value = {"Items": []}

        fanout_entity(client, "test-table", "ns123", "vip-1", resource="gpt-4", disabled=False)

        kwargs = client.query.call_args.kwargs
        assert kwargs["ExpressionAttributeValues"][":sk"] == {"S": "BUCKET#gpt-4#"}

    def test_stamps_discovered_entity_buckets_with_disabled_false(self):
        """disabled=False (carve-out / re-enable) removes the attribute per bucket."""
        client = _make_client()
        client.query.return_value = {"Items": [{"PK": {"S": "ns123/BUCKET#vip-1#gpt-4#0"}}]}

        count = fanout_entity(
            client, "test-table", "ns123", "vip-1", resource="gpt-4", disabled=False
        )

        assert count == 1
        kwargs = client.update_item.call_args.kwargs
        assert kwargs["UpdateExpression"] == "REMOVE #disabled"

    def test_paginates_via_last_evaluated_key(self):
        client = _make_client()
        page1_key = {"PK": {"S": "ns123/BUCKET#vip-1#gpt-4#0"}, "SK": {"S": "#STATE"}}
        client.query.side_effect = [
            {
                "Items": [{"PK": {"S": "ns123/BUCKET#vip-1#gpt-4#0"}}],
                "LastEvaluatedKey": page1_key,
            },
            {"Items": [{"PK": {"S": "ns123/BUCKET#vip-1#claude-3#0"}}]},
            # Second pass (Finding 2): nothing new discovered.
            {"Items": []},
        ]

        count = fanout_entity(client, "test-table", "ns123", "vip-1", resource=None, disabled=True)

        assert count == 2
        assert client.query.call_count == 3
        assert client.query.call_args_list[1].kwargs["ExclusiveStartKey"] == page1_key
        assert "ExclusiveStartKey" not in client.query.call_args_list[2].kwargs

    def test_bucket_vanishing_mid_fanout_does_not_raise_or_short_circuit(self):
        """A ConditionalCheckFailedException on one bucket must not abort the rest."""
        client = _make_client()
        client.query.return_value = {
            "Items": [
                {"PK": {"S": "ns123/BUCKET#vip-1#gpt-4#0"}},
                {"PK": {"S": "ns123/BUCKET#vip-1#gpt-4#1"}},
            ]
        }
        client.update_item.side_effect = [
            ConditionalCheckFailedException(),
            None,
        ]

        count = fanout_entity(
            client, "test-table", "ns123", "vip-1", resource="gpt-4", disabled=True
        )

        # Both discovered PKs count as "stamped" (attempted); the vanished one
        # just silently no-ops rather than raising.
        assert count == 2
        assert client.update_item.call_count == 2


class TestFanoutEntityUnscopedOverride:
    """Tests for fanout_entity's per-resource override check when `resource=None`.

    An unscoped call (the entity's `_default_` directive) discovers buckets
    across every resource the entity has one for. A resource-specific
    override for this same entity must still win over that entity-wide
    directive -- mirroring `Repository._fanout_entity`'s
    `effective_by_resource` check -- so an entity carve-out on one resource
    is never clobbered by disabling everything else.
    """

    NS = "ns123"
    ENTITY = "vip-1"

    def test_stamps_buckets_across_multiple_resources_when_no_override(self):
        """Two real resources, neither with its own override: both inherit
        the entity-wide `_default_` directive and get stamped."""
        client = _make_client()
        client.query.return_value = {
            "Items": [
                {"PK": {"S": "ns123/BUCKET#vip-1#gpt-4#0"}},
                {"PK": {"S": "ns123/BUCKET#vip-1#claude-3#0"}},
            ]
        }
        client.get_item.side_effect = _item_router(
            {(pk_entity(self.NS, self.ENTITY), sk_config(DEFAULT_RESOURCE)): True}
        )

        count = fanout_entity(
            client, "test-table", self.NS, self.ENTITY, resource=None, disabled=True
        )

        assert count == 2
        stamped_pks = {c.kwargs["Key"]["PK"]["S"] for c in client.update_item.call_args_list}
        assert stamped_pks == {
            "ns123/BUCKET#vip-1#gpt-4#0",
            "ns123/BUCKET#vip-1#claude-3#0",
        }

    def test_resource_specific_override_wins_over_entity_wide_directive(self):
        """An entity's own explicit `disabled: false` for one resource must not
        be clobbered by an entity-wide `disabled: true` directive (ADR-125).

        Every bucket is stamped with its OWN resolved value, so claude-3 gets a
        REMOVE (its carve-out resolves False) while gpt-4 gets a SET.
        """
        client = _make_client()
        client.query.return_value = {
            "Items": [
                {"PK": {"S": "ns123/BUCKET#vip-1#gpt-4#0"}},
                {"PK": {"S": "ns123/BUCKET#vip-1#claude-3#0"}},
            ]
        }
        client.get_item.side_effect = _item_router(
            {
                (pk_entity(self.NS, self.ENTITY), sk_config(DEFAULT_RESOURCE)): True,
                # vip-1's own carve-out for claude-3 specifically.
                (pk_entity(self.NS, self.ENTITY), sk_config("claude-3")): False,
            }
        )

        count = fanout_entity(
            client, "test-table", self.NS, self.ENTITY, resource=None, disabled=True
        )

        assert count == 2
        by_pk = {
            c.kwargs["Key"]["PK"]["S"]: c.kwargs["UpdateExpression"]
            for c in client.update_item.call_args_list
        }
        assert by_pk["ns123/BUCKET#vip-1#gpt-4#0"] == "SET #disabled = :true"
        # The carve-out is preserved as "not disabled", never stamped True.
        assert by_pk["ns123/BUCKET#vip-1#claude-3#0"] == "REMOVE #disabled"

    def test_clear_restamps_resource_whose_own_config_now_decides(self):
        """Regression (Critical 1): unscoped clear must stamp a bucket that
        must BECOME disabled.

        After the entity's `_default_` carve-out is removed, gpt-4's own
        resource-level `disabled: true` becomes the deciding level. Skipping
        buckets whose resolution disagrees with the directive would leave the
        bucket unstamped forever — a kill-switch bypass.
        """
        client = _make_client()
        client.query.return_value = {"Items": [{"PK": {"S": "ns123/BUCKET#vip-1#gpt-4#0"}}]}
        # The entity `_default_` value is already gone; gpt-4 says disabled.
        client.get_item.side_effect = _item_router(
            {(pk_resource(self.NS, "gpt-4"), sk_config()): True}
        )

        # The directive being applied is "not disabled" (what `_default_` now
        # inherits), but gpt-4 resolves to disabled and must be stamped.
        count = fanout_entity(
            client, "test-table", self.NS, self.ENTITY, resource=None, disabled=False
        )

        assert count == 1
        kwargs = client.update_item.call_args.kwargs
        assert kwargs["Key"]["PK"]["S"] == "ns123/BUCKET#vip-1#gpt-4#0"
        assert kwargs["UpdateExpression"] == "SET #disabled = :true"

    def test_scoped_call_skips_the_override_check_entirely(self):
        """When `resource` is given (not None), the caller's directive is
        unambiguous, so no `get_item` calls are made at all."""
        client = _make_client()
        client.query.return_value = {"Items": [{"PK": {"S": "ns123/BUCKET#vip-1#gpt-4#0"}}]}

        count = fanout_entity(
            client, "test-table", self.NS, self.ENTITY, resource="gpt-4", disabled=True
        )

        assert count == 1
        client.get_item.assert_not_called()


class TestResolveDisabled:
    """Tests for resolve_disabled: the sync boto3 mirror of Repository.resolve_disabled.

    Walks entity(resource) -> entity(_default_) -> resource, first explicit
    value wins. Used by the handler to compute the effective stamp value for
    an entity-level provisioner change (Finding 1), instead of the wrong
    `data.get("disabled", False)` shortcut.
    """

    NS = "ns123"
    ENTITY = "vip-1"
    RESOURCE = "gpt-4"

    def _no_item(self):
        return {}

    def _item(self, value: bool):
        return {"Item": {"disabled": {"BOOL": value}}}

    def test_nothing_set_resolves_false(self):
        client = _make_client()
        client.get_item.return_value = self._no_item()

        result = resolve_disabled(client, "test-table", self.NS, self.ENTITY, self.RESOURCE)

        assert result is False
        # All three levels are consulted when none of them decide.
        assert client.get_item.call_count == 3
        keys = [c.kwargs["Key"] for c in client.get_item.call_args_list]
        assert keys[0] == {
            "PK": {"S": pk_entity(self.NS, self.ENTITY)},
            "SK": {"S": sk_config(self.RESOURCE)},
        }
        assert keys[1] == {
            "PK": {"S": pk_entity(self.NS, self.ENTITY)},
            "SK": {"S": sk_config(DEFAULT_RESOURCE)},
        }
        assert keys[2] == {
            "PK": {"S": pk_resource(self.NS, self.RESOURCE)},
            "SK": {"S": sk_config()},
        }

    def test_entity_resource_level_true_wins_without_further_lookups(self):
        client = _make_client()
        client.get_item.return_value = self._item(True)

        result = resolve_disabled(client, "test-table", self.NS, self.ENTITY, self.RESOURCE)

        assert result is True
        # First level decides -> entity(_default_) and resource are never queried.
        assert client.get_item.call_count == 1

    def test_entity_default_level_used_when_own_resource_absent(self):
        client = _make_client()
        client.get_item.side_effect = [self._no_item(), self._item(True), self._no_item()]

        result = resolve_disabled(client, "test-table", self.NS, self.ENTITY, self.RESOURCE)

        assert result is True
        assert client.get_item.call_count == 2

    def test_resource_level_fallback_when_both_entity_levels_absent(self):
        client = _make_client()
        client.get_item.side_effect = [self._no_item(), self._no_item(), self._item(True)]

        result = resolve_disabled(client, "test-table", self.NS, self.ENTITY, self.RESOURCE)

        assert result is True
        assert client.get_item.call_count == 3

    def test_entity_own_false_beats_resource_true(self):
        """The tri-state carve-out: an entity's own explicit False must win over
        a resource-level True, never be conflated with 'absent'."""
        client = _make_client()
        client.get_item.side_effect = [self._item(False), self._no_item(), self._item(True)]

        result = resolve_disabled(client, "test-table", self.NS, self.ENTITY, self.RESOURCE)

        assert result is False
        # Decided at the first level -> resource level is never even queried.
        assert client.get_item.call_count == 1

    def test_default_resource_skips_entity_default_level(self):
        """When resource IS `_default_`, the entity(_default_) level would be a
        duplicate of entity(resource) and is skipped, matching the async walk."""
        client = _make_client()
        client.get_item.return_value = self._no_item()

        resolve_disabled(client, "test-table", self.NS, self.ENTITY, DEFAULT_RESOURCE)

        assert client.get_item.call_count == 2
        keys = [c.kwargs["Key"] for c in client.get_item.call_args_list]
        assert keys[0]["SK"] == {"S": sk_config(DEFAULT_RESOURCE)}
        assert keys[1] == {
            "PK": {"S": pk_resource(self.NS, DEFAULT_RESOURCE)},
            "SK": {"S": sk_config()},
        }
