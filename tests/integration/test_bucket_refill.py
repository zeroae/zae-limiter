"""Integration tests for aggregator bucket refill against real DynamoDB.

Tests verify that try_refill_bucket() works correctly with actual DynamoDB
(via LocalStack), including ADD commutativity with speculative writes and
the optimistic lock on the shared rf timestamp.

See: https://github.com/zeroae/zae-limiter/issues/317
"""

import os
import time
import uuid

import boto3
import pytest

from zae_limiter.schema import (
    BUCKET_FIELD_CP,
    BUCKET_FIELD_RA,
    BUCKET_FIELD_RP,
    BUCKET_FIELD_TC,
    BUCKET_FIELD_TK,
    bucket_attr,
    get_table_definition,
    pk_entity,
    sk_bucket,
)
from zae_limiter_aggregator.processor import (
    BucketRefillState,
    LimitRefillInfo,
    aggregate_bucket_states,
    try_refill_bucket,
)


@pytest.fixture(scope="module")
def dynamodb_table():
    """Create a DynamoDB table for testing."""
    endpoint_url = os.getenv("AWS_ENDPOINT_URL")
    if not endpoint_url:
        pytest.skip("AWS_ENDPOINT_URL not set - LocalStack not available")

    table_name = f"test-refill-{uuid.uuid4().hex[:8]}"

    dynamodb = boto3.resource(
        "dynamodb",
        endpoint_url=endpoint_url,
        region_name="us-east-1",
    )

    table_def = get_table_definition(table_name)
    table = dynamodb.create_table(**table_def)
    table.wait_until_exists()

    yield table

    table.delete()


def _seed_bucket(table, entity_id: str, resource: str, limits: dict, rf_ms: int):
    """Seed a composite bucket item (simulates what a speculative write creates).

    Args:
        table: boto3 Table resource
        entity_id: Entity ID
        resource: Resource name
        limits: Dict of limit_name -> {tk, cp, ra, rp, tc} in millitokens/ms
        rf_ms: Shared refill timestamp
    """
    item = {
        "PK": pk_entity("default", entity_id),
        "SK": sk_bucket(resource),
        "entity_id": entity_id,
        "rf": rf_ms,
    }
    for limit_name, fields in limits.items():
        item[bucket_attr(limit_name, BUCKET_FIELD_TK)] = fields["tk"]
        item[bucket_attr(limit_name, BUCKET_FIELD_CP)] = fields["cp"]
        item[bucket_attr(limit_name, BUCKET_FIELD_RA)] = fields["ra"]
        item[bucket_attr(limit_name, BUCKET_FIELD_RP)] = fields["rp"]
        item[bucket_attr(limit_name, BUCKET_FIELD_TC)] = fields["tc"]

    table.put_item(Item=item)


def _get_bucket(table, entity_id: str, resource: str) -> dict:
    """Read a bucket item from DynamoDB."""
    response = table.get_item(
        Key={
            "PK": pk_entity("default", entity_id),
            "SK": sk_bucket(resource),
        }
    )
    return response.get("Item", {})


def _make_stream_record(
    entity_id: str,
    resource: str,
    limits_old: dict,
    limits_new: dict,
    rf_ms: int,
) -> dict:
    """Build a DynamoDB stream MODIFY record for a composite bucket.

    Args:
        entity_id: Entity ID
        resource: Resource name
        limits_old: Dict of limit_name -> {tk, cp, ra, rp, tc} for OldImage
        limits_new: Dict of limit_name -> {tk, cp, ra, rp, tc} for NewImage
        rf_ms: Shared refill timestamp in NewImage
    """

    def _build_image(limits: dict, rf: int) -> dict:
        image = {
            "PK": {"S": pk_entity("default", entity_id)},
            "SK": {"S": sk_bucket(resource)},
            "entity_id": {"S": entity_id},
            "rf": {"N": str(rf)},
        }
        for limit_name, fields in limits.items():
            image[bucket_attr(limit_name, BUCKET_FIELD_TK)] = {"N": str(fields["tk"])}
            image[bucket_attr(limit_name, BUCKET_FIELD_CP)] = {"N": str(fields["cp"])}
            image[bucket_attr(limit_name, BUCKET_FIELD_RA)] = {"N": str(fields["ra"])}
            image[bucket_attr(limit_name, BUCKET_FIELD_RP)] = {"N": str(fields["rp"])}
            image[bucket_attr(limit_name, BUCKET_FIELD_TC)] = {"N": str(fields["tc"])}
        return image

    return {
        "eventName": "MODIFY",
        "dynamodb": {
            "OldImage": _build_image(limits_old, rf_ms),
            "NewImage": _build_image(limits_new, rf_ms),
        },
    }


@pytest.mark.integration
class TestTryRefillBucketIntegration:
    """Integration tests for try_refill_bucket against real DynamoDB."""

    def test_refill_writes_add_tokens(self, dynamodb_table) -> None:
        """Refill ADD increases tokens in DynamoDB when bucket is depleted."""
        entity_id = f"entity-{uuid.uuid4().hex[:8]}"
        resource = "gpt-4"
        # Only 10s elapsed: refill covers ~1.67k of the 10k consumed, so
        # projected (~1.67k) < consumption_estimate (10k) → triggers refill
        old_rf_ms = int(time.time() * 1000) - 10_000  # 10s ago

        # Seed a depleted bucket: 0 tokens, capacity 10k, refills 10k/min
        _seed_bucket(
            dynamodb_table,
            entity_id,
            resource,
            limits={
                "tpm": {
                    "tk": 0,  # depleted
                    "cp": 10_000_000,  # 10k capacity
                    "ra": 10_000_000,  # 10k refill amount
                    "rp": 60_000,  # 60s period
                    "tc": 10_000_000,  # consumed 10k
                },
            },
            rf_ms=old_rf_ms,
        )

        # Build state as if aggregator parsed the stream
        state = BucketRefillState(
            namespace_id="default",
            entity_id=entity_id,
            resource=resource,
            rf_ms=old_rf_ms,
            limits={
                "tpm": LimitRefillInfo(
                    tc_delta=10_000_000,  # consumed 10k in batch
                    tk_milli=0,
                    cp_milli=10_000_000,
                    ra_milli=10_000_000,
                    rp_ms=60_000,
                ),
            },
        )

        now_ms = int(time.time() * 1000)
        result = try_refill_bucket(dynamodb_table, state, now_ms)

        assert result is True

        # Verify tokens were refilled in DynamoDB
        item = _get_bucket(dynamodb_table, entity_id, resource)
        assert item["b_tpm_tk"] > 0, "Tokens should have been refilled"
        assert item["rf"] == now_ms, "rf should be updated to now_ms"

    def test_refill_skipped_when_sufficient_tokens(self, dynamodb_table) -> None:
        """Refill skipped when projected tokens exceed consumption estimate."""
        entity_id = f"entity-{uuid.uuid4().hex[:8]}"
        resource = "gpt-4"
        old_rf_ms = int(time.time() * 1000) - 60_000

        # Seed bucket with plenty of tokens
        _seed_bucket(
            dynamodb_table,
            entity_id,
            resource,
            limits={
                "tpm": {
                    "tk": 9_000_000,  # 9k tokens remaining
                    "cp": 10_000_000,
                    "ra": 10_000_000,
                    "rp": 60_000,
                    "tc": 1_000_000,
                },
            },
            rf_ms=old_rf_ms,
        )

        state = BucketRefillState(
            namespace_id="default",
            entity_id=entity_id,
            resource=resource,
            rf_ms=old_rf_ms,
            limits={
                "tpm": LimitRefillInfo(
                    tc_delta=1_000_000,  # only consumed 1k
                    tk_milli=9_000_000,  # plenty left
                    cp_milli=10_000_000,
                    ra_milli=10_000_000,
                    rp_ms=60_000,
                ),
            },
        )

        now_ms = int(time.time() * 1000)
        result = try_refill_bucket(dynamodb_table, state, now_ms)

        assert result is False

        # Verify rf was NOT changed
        item = _get_bucket(dynamodb_table, entity_id, resource)
        assert item["rf"] == old_rf_ms, "rf should not have changed"

    def test_optimistic_lock_prevents_double_refill(self, dynamodb_table) -> None:
        """Second refill with stale rf fails gracefully (ConditionalCheckFailed)."""
        entity_id = f"entity-{uuid.uuid4().hex[:8]}"
        resource = "gpt-4"
        old_rf_ms = int(time.time() * 1000) - 10_000  # 10s ago

        _seed_bucket(
            dynamodb_table,
            entity_id,
            resource,
            limits={
                "tpm": {
                    "tk": 0,
                    "cp": 10_000_000,
                    "ra": 10_000_000,
                    "rp": 60_000,
                    "tc": 10_000_000,
                },
            },
            rf_ms=old_rf_ms,
        )

        state = BucketRefillState(
            namespace_id="default",
            entity_id=entity_id,
            resource=resource,
            rf_ms=old_rf_ms,
            limits={
                "tpm": LimitRefillInfo(
                    tc_delta=10_000_000,
                    tk_milli=0,
                    cp_milli=10_000_000,
                    ra_milli=10_000_000,
                    rp_ms=60_000,
                ),
            },
        )

        now_ms = int(time.time() * 1000)

        # First refill succeeds
        assert try_refill_bucket(dynamodb_table, state, now_ms) is True

        # Second refill with same stale rf_ms should fail (rf already updated)
        assert try_refill_bucket(dynamodb_table, state, now_ms + 1000) is False

    def test_add_commutes_with_concurrent_consume(self, dynamodb_table) -> None:
        """Refill ADD and speculative consume ADD commute correctly.

        Simulates: speculative write consumes 1k tokens concurrently with
        aggregator refilling tokens. Final result should reflect both.
        """
        entity_id = f"entity-{uuid.uuid4().hex[:8]}"
        resource = "gpt-4"
        old_rf_ms = int(time.time() * 1000) - 10_000  # 10s ago

        # Seed depleted bucket: low tokens, high consumption
        initial_tk = 500_000  # 500 tokens remaining
        _seed_bucket(
            dynamodb_table,
            entity_id,
            resource,
            limits={
                "tpm": {
                    "tk": initial_tk,
                    "cp": 10_000_000,
                    "ra": 10_000_000,
                    "rp": 60_000,
                    "tc": 9_500_000,
                },
            },
            rf_ms=old_rf_ms,
        )

        # Simulate concurrent speculative consume (ADD -1000_000)
        dynamodb_table.update_item(
            Key={
                "PK": pk_entity("default", entity_id),
                "SK": sk_bucket(resource),
            },
            UpdateExpression="ADD b_tpm_tk :consumed, b_tpm_tc :consumed_tc",
            ExpressionAttributeValues={
                ":consumed": -1_000_000,  # consume 1k tokens
                ":consumed_tc": 1_000_000,
            },
        )

        # Now aggregator refills — the ADD should commute
        # tc_delta=10M ensures projected (~1.67M) < consumption (10M) → triggers refill
        state = BucketRefillState(
            namespace_id="default",
            entity_id=entity_id,
            resource=resource,
            rf_ms=old_rf_ms,
            limits={
                "tpm": LimitRefillInfo(
                    tc_delta=10_000_000,  # high consumption rate
                    tk_milli=initial_tk,  # from stream snapshot (before concurrent consume)
                    cp_milli=10_000_000,
                    ra_milli=10_000_000,
                    rp_ms=60_000,
                ),
            },
        )

        now_ms = int(time.time() * 1000)
        assert try_refill_bucket(dynamodb_table, state, now_ms) is True

        # Verify final state: initial - consume + refill
        item = _get_bucket(dynamodb_table, entity_id, resource)
        final_tk = int(item["b_tpm_tk"])

        # initial(500k) - consume(1000k) + refill(~1667k) should be positive
        # The exact refill depends on elapsed time, but it should be > initial - consume
        assert final_tk > initial_tk - 1_000_000, (
            f"Tokens should reflect both consume and refill: {final_tk}"
        )

    def test_multiple_limits_single_update(self, dynamodb_table) -> None:
        """Refill writes all limits in a single UpdateItem."""
        entity_id = f"entity-{uuid.uuid4().hex[:8]}"
        resource = "gpt-4"
        old_rf_ms = int(time.time() * 1000) - 10_000  # 10s ago

        _seed_bucket(
            dynamodb_table,
            entity_id,
            resource,
            limits={
                "tpm": {
                    "tk": 0,
                    "cp": 10_000_000,
                    "ra": 10_000_000,
                    "rp": 60_000,
                    "tc": 10_000_000,
                },
                "rpm": {
                    "tk": 0,
                    "cp": 1_000_000,
                    "ra": 1_000_000,
                    "rp": 60_000,
                    "tc": 1_000_000,
                },
            },
            rf_ms=old_rf_ms,
        )

        state = BucketRefillState(
            namespace_id="default",
            entity_id=entity_id,
            resource=resource,
            rf_ms=old_rf_ms,
            limits={
                "tpm": LimitRefillInfo(
                    tc_delta=10_000_000,
                    tk_milli=0,
                    cp_milli=10_000_000,
                    ra_milli=10_000_000,
                    rp_ms=60_000,
                ),
                "rpm": LimitRefillInfo(
                    tc_delta=1_000_000,
                    tk_milli=0,
                    cp_milli=1_000_000,
                    ra_milli=1_000_000,
                    rp_ms=60_000,
                ),
            },
        )

        now_ms = int(time.time() * 1000)
        assert try_refill_bucket(dynamodb_table, state, now_ms) is True

        item = _get_bucket(dynamodb_table, entity_id, resource)
        assert int(item["b_tpm_tk"]) > 0, "tpm tokens should be refilled"
        assert int(item["b_rpm_tk"]) > 0, "rpm tokens should be refilled"
        assert item["rf"] == now_ms


@pytest.mark.integration
class TestAggregateAndRefillIntegration:
    """End-to-end: stream records → aggregate → refill → verify DynamoDB."""

    def test_stream_to_refill_pipeline(self, dynamodb_table) -> None:
        """Full pipeline: parse stream records, aggregate, and refill bucket."""
        entity_id = f"entity-{uuid.uuid4().hex[:8]}"
        resource = "claude-3"
        old_rf_ms = int(time.time() * 1000) - 10_000  # 10s ago

        # Seed a depleted bucket
        _seed_bucket(
            dynamodb_table,
            entity_id,
            resource,
            limits={
                "tpm": {
                    "tk": 0,
                    "cp": 10_000_000,
                    "ra": 10_000_000,
                    "rp": 60_000,
                    "tc": 20_000_000,  # consumed 20k total
                },
            },
            rf_ms=old_rf_ms,
        )

        # Build stream records simulating 2 speculative writes
        records = [
            _make_stream_record(
                entity_id,
                resource,
                limits_old={
                    "tpm": {
                        "tk": 5_000_000,
                        "cp": 10_000_000,
                        "ra": 10_000_000,
                        "rp": 60_000,
                        "tc": 15_000_000,
                    },
                },
                limits_new={
                    "tpm": {
                        "tk": 2_000_000,
                        "cp": 10_000_000,
                        "ra": 10_000_000,
                        "rp": 60_000,
                        "tc": 18_000_000,  # consumed 3k
                    },
                },
                rf_ms=old_rf_ms,
            ),
            _make_stream_record(
                entity_id,
                resource,
                limits_old={
                    "tpm": {
                        "tk": 2_000_000,
                        "cp": 10_000_000,
                        "ra": 10_000_000,
                        "rp": 60_000,
                        "tc": 18_000_000,
                    },
                },
                limits_new={
                    "tpm": {
                        "tk": 0,
                        "cp": 10_000_000,
                        "ra": 10_000_000,
                        "rp": 60_000,
                        "tc": 20_000_000,  # consumed 2k more = 5k total
                    },
                },
                rf_ms=old_rf_ms,
            ),
        ]

        # Aggregate states from stream records
        bucket_states = aggregate_bucket_states(records)
        assert len(bucket_states) == 1

        key = ("default", entity_id, resource)
        state = bucket_states[key]

        # Verify aggregation: tc_delta = 5k across both events
        assert state.limits["tpm"].tc_delta == 5_000_000
        # Last NewImage: tk=0
        assert state.limits["tpm"].tk_milli == 0

        # Refill the bucket
        now_ms = int(time.time() * 1000)
        assert try_refill_bucket(dynamodb_table, state, now_ms) is True

        # Verify DynamoDB was updated
        item = _get_bucket(dynamodb_table, entity_id, resource)
        assert int(item["b_tpm_tk"]) > 0, "Tokens should be refilled after pipeline"
        assert item["rf"] == now_ms
