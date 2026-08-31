"""Tests for the provisioner's sync boto3 bucket fan-out (ADR-125).

Mirrors the mocking conventions in test_provisioner_handler.py / test_applier.py:
a MagicMock boto3 DynamoDB client, with `client.exceptions.*` populated with real
exception classes so `except client.exceptions.X` matches in tests the same way it
does against a real boto3 client.
"""

from unittest.mock import MagicMock

from zae_limiter_provisioner.fanout import fanout_entity, fanout_resource, stamp_bucket

# Use type() to create a class with the AWS name (avoids N818 lint rule),
# matching the convention in test_limiter.py's TestLeaseRetryPath.
ConditionalCheckFailedException = type("ConditionalCheckFailedException", (Exception,), {})


def _make_client() -> MagicMock:
    client = MagicMock()
    client.exceptions.ConditionalCheckFailedException = ConditionalCheckFailedException
    return client


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
        client.query.assert_called_once()
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
        ]

        count = fanout_resource(client, "test-table", "ns123", "gpt-4", disabled=True)

        assert count == 2
        assert client.query.call_count == 2
        first_call_kwargs = client.query.call_args_list[0].kwargs
        second_call_kwargs = client.query.call_args_list[1].kwargs
        assert "ExclusiveStartKey" not in first_call_kwargs
        assert second_call_kwargs["ExclusiveStartKey"] == page1_key

    def test_deduplicates_pk_seen_across_pages(self):
        """The same PK returned on two pages is stamped only once."""
        client = _make_client()
        dup_key = {"PK": {"S": "ns123/BUCKET#user-1#gpt-4#0"}, "SK": {"S": "#STATE"}}
        client.query.side_effect = [
            {
                "Items": [{"PK": {"S": "ns123/BUCKET#user-1#gpt-4#0"}}],
                "LastEvaluatedKey": dup_key,
            },
            {"Items": [{"PK": {"S": "ns123/BUCKET#user-1#gpt-4#0"}}]},
        ]

        count = fanout_resource(client, "test-table", "ns123", "gpt-4", disabled=True)

        assert count == 1
        assert client.update_item.call_count == 1


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
        ]

        count = fanout_entity(client, "test-table", "ns123", "vip-1", resource=None, disabled=True)

        assert count == 2
        assert client.query.call_count == 2
        assert client.query.call_args_list[1].kwargs["ExclusiveStartKey"] == page1_key

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
