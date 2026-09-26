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

from zae_limiter.schedule import ScheduleEntry, decode, encode
from zae_limiter.schema import (
    BUCKET_FIELD_CP,
    BUCKET_FIELD_RA,
    BUCKET_FIELD_RP,
    BUCKET_FIELD_RSA,
    BUCKET_FIELD_SCHED,
    BUCKET_FIELD_SCHED_TZ,
    BUCKET_FIELD_TC,
    BUCKET_FIELD_TK,
    BUCKET_FIELD_VU,
    BUCKET_FIELD_WS,
    BUCKET_SCHED_NONE,
    bucket_attr,
    get_table_definition,
    pk_bucket,
    sk_state,
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


def _seed_bucket(table, entity_id: str, resource: str, limits: dict, rf_ms: int, shard_id: int = 0):
    """Seed a composite bucket item (simulates what a speculative write creates).

    Args:
        table: boto3 Table resource
        entity_id: Entity ID
        resource: Resource name
        limits: Dict of limit_name -> {tk, cp, ra, rp, tc} in millitokens/ms
        rf_ms: Shared refill timestamp
        shard_id: Shard index (default 0)
    """
    item = {
        "PK": pk_bucket("default", entity_id, resource, shard_id),
        "SK": sk_state(),
        "entity_id": entity_id,
        "rf": rf_ms,
        "shard_count": 1,
    }
    for limit_name, fields in limits.items():
        item[bucket_attr(limit_name, BUCKET_FIELD_TK)] = fields["tk"]
        item[bucket_attr(limit_name, BUCKET_FIELD_CP)] = fields["cp"]
        item[bucket_attr(limit_name, BUCKET_FIELD_RA)] = fields["ra"]
        item[bucket_attr(limit_name, BUCKET_FIELD_RP)] = fields["rp"]
        item[bucket_attr(limit_name, BUCKET_FIELD_TC)] = fields["tc"]

    table.put_item(Item=item)


def _get_bucket(table, entity_id: str, resource: str, shard_id: int = 0) -> dict:
    """Read a bucket item from DynamoDB."""
    response = table.get_item(
        Key={
            "PK": pk_bucket("default", entity_id, resource, shard_id),
            "SK": sk_state(),
        }
    )
    return response.get("Item", {})


def _make_stream_record(
    entity_id: str,
    resource: str,
    limits_old: dict,
    limits_new: dict,
    rf_ms: int,
    shard_id: int = 0,
) -> dict:
    """Build a DynamoDB stream MODIFY record for a composite bucket.

    Args:
        entity_id: Entity ID
        resource: Resource name
        limits_old: Dict of limit_name -> {tk, cp, ra, rp, tc} for OldImage
        limits_new: Dict of limit_name -> {tk, cp, ra, rp, tc} for NewImage
        rf_ms: Shared refill timestamp in NewImage
        shard_id: Shard index (default 0)
    """

    def _build_image(limits: dict, rf: int) -> dict:
        image = {
            "PK": {"S": pk_bucket("default", entity_id, resource, shard_id)},
            "SK": {"S": sk_state()},
            "entity_id": {"S": entity_id},
            "rf": {"N": str(rf)},
            "shard_count": {"N": "1"},
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
                "PK": pk_bucket("default", entity_id, resource, 0),
                "SK": sk_state(),
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

        key = ("default", entity_id, resource, 0)
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


# Always active, so the assertions below do not depend on the wall clock.
ALWAYS_HALF = (ScheduleEntry(cron="* * * * *", tz="UTC", scale=0.5),)
ALWAYS_HALF_COMPACT, _ALWAYS_HALF_TZ = encode(ALWAYS_HALF)
ALWAYS_QUARTER_COMPACT = encode((ScheduleEntry(cron="* * * * *", tz="UTC", scale=0.25),))[0]


@pytest.mark.integration
class TestScheduledRefillIntegration:
    """Scheduled refill and `vu` re-stamping against real DynamoDB (#222).

    The unit tests assert what is *passed* to update_item. These assert that
    DynamoDB accepts it: the combined ``SET rf, #vu ADD ...`` expression and the
    two-clause condition are the parts a MagicMock cannot validate.
    """

    def _seed(self, table, entity_id, resource, rf_ms, *, sched, vu_ms):
        _seed_bucket(
            table,
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
            rf_ms=rf_ms,
        )
        table.update_item(
            Key={"PK": pk_bucket("default", entity_id, resource, 0), "SK": sk_state()},
            UpdateExpression="SET #sched = :s, #tz = :tz, #vu = :vu",
            ExpressionAttributeNames={
                "#sched": BUCKET_FIELD_SCHED,
                "#tz": BUCKET_FIELD_SCHED_TZ,
                "#vu": BUCKET_FIELD_VU,
            },
            ExpressionAttributeValues={":s": sched, ":tz": "UTC", ":vu": vu_ms},
        )

    def _state(self, entity_id, resource, rf_ms, *, sched_compact, vu_ms):
        return BucketRefillState(
            namespace_id="default",
            entity_id=entity_id,
            resource=resource,
            rf_ms=rf_ms,
            limits={
                "tpm": LimitRefillInfo(
                    tc_delta=10_000_000,
                    tk_milli=0,
                    cp_milli=10_000_000,
                    ra_milli=10_000_000,
                    rp_ms=60_000,
                    sched=decode(sched_compact, "UTC"),
                ),
            },
            sched=decode(sched_compact, "UTC"),
            sched_compact=sched_compact,
            vu_ms=vu_ms,
        )

    def test_refills_to_the_scheduled_share_and_advances_vu(self, dynamodb_table) -> None:
        entity_id = f"entity-{uuid.uuid4().hex[:8]}"
        resource = "gpt-4"
        now_ms = int(time.time() * 1000)
        old_rf_ms = now_ms - 60_000

        self._seed(
            dynamodb_table,
            entity_id,
            resource,
            old_rf_ms,
            sched=ALWAYS_HALF_COMPACT,
            vu_ms=now_ms - 1,
        )
        state = self._state(
            entity_id,
            resource,
            old_rf_ms,
            sched_compact=ALWAYS_HALF_COMPACT,
            vu_ms=now_ms - 1,
        )

        assert try_refill_bucket(dynamodb_table, state, now_ms) is True

        item = _get_bucket(dynamodb_table, entity_id, resource)
        assert item["b_tpm_tk"] == 5_000_000, "one minute at the halved rate"
        assert item["rf"] == now_ms
        assert item[BUCKET_FIELD_VU] > now_ms, "vu must advance past now"

    def test_a_schedule_changed_underneath_blocks_the_whole_write(self, dynamodb_table) -> None:
        """The #468 fan-out rewrites `sched` and sets `vu = 0` without touching
        `rf`, so the rf lock alone cannot tell a pre-fan-out stream image from a
        current one. Re-stamping `vu` off the stale image would push it back
        into the future and cancel the materialising pass `vu = 0` forced."""
        entity_id = f"entity-{uuid.uuid4().hex[:8]}"
        resource = "gpt-4"
        now_ms = int(time.time() * 1000)
        old_rf_ms = now_ms - 60_000

        # The item is on the 0.25x schedule; the stream image the aggregator
        # is working from still says 0.5x.
        self._seed(
            dynamodb_table,
            entity_id,
            resource,
            old_rf_ms,
            sched=ALWAYS_QUARTER_COMPACT,
            vu_ms=0,
        )
        state = self._state(
            entity_id, resource, old_rf_ms, sched_compact=ALWAYS_HALF_COMPACT, vu_ms=0
        )

        assert try_refill_bucket(dynamodb_table, state, now_ms) is False

        item = _get_bucket(dynamodb_table, entity_id, resource)
        assert item["rf"] == old_rf_ms, "nothing was written"
        assert item[BUCKET_FIELD_VU] == 0, "the forced pass is still pending"

    def test_the_same_write_lands_when_the_schedule_still_matches(self, dynamodb_table) -> None:
        """Discriminates the test above: identical setup, current image."""
        entity_id = f"entity-{uuid.uuid4().hex[:8]}"
        resource = "gpt-4"
        now_ms = int(time.time() * 1000)
        old_rf_ms = now_ms - 60_000

        self._seed(
            dynamodb_table,
            entity_id,
            resource,
            old_rf_ms,
            sched=ALWAYS_QUARTER_COMPACT,
            vu_ms=0,
        )
        state = self._state(
            entity_id, resource, old_rf_ms, sched_compact=ALWAYS_QUARTER_COMPACT, vu_ms=0
        )

        assert try_refill_bucket(dynamodb_table, state, now_ms) is True

        item = _get_bucket(dynamodb_table, entity_id, resource)
        assert item["b_tpm_tk"] == 2_500_000
        assert item[BUCKET_FIELD_VU] > now_ms


@pytest.mark.integration
class TestMixedScheduleItemIntegration:
    """#541 against real DynamoDB: the marker round-trips and is honoured.

    ``BUCKET_SCHED_NONE`` is a reserved string attribute value, so this is the
    part a MagicMock cannot validate — that DynamoDB stores ``"-"`` and returns
    it unchanged in a stream image, and that the refill it drives lands.

    ``tpm`` is unscheduled and ``spm`` shares the item's always-on ``0.5x``
    window. Both are empty, both have consumed their whole capacity, and both
    are 30 seconds stale — half a refill period, which keeps each projection
    below the consumption estimate so neither is skipped by the threshold. The
    two deltas then separate the readings exactly: half of 10,000,000 at the
    base rate, half of the halved 5,000,000 at the scheduled one.
    """

    LIMITS = {
        "tpm": {"tk": 0, "cp": 10_000_000, "ra": 10_000_000, "rp": 60_000, "tc": 10_000_000},
        "spm": {"tk": 0, "cp": 10_000_000, "ra": 10_000_000, "rp": 60_000, "tc": 10_000_000},
    }

    def test_an_unscheduled_limit_refills_at_its_base_rate(self, dynamodb_table) -> None:
        entity_id = f"entity-{uuid.uuid4().hex[:8]}"
        resource = "gpt-4"
        now_ms = int(time.time() * 1000)
        old_rf_ms = now_ms - 30_000

        _seed_bucket(dynamodb_table, entity_id, resource, limits=self.LIMITS, rf_ms=old_rf_ms)
        dynamodb_table.update_item(
            Key={"PK": pk_bucket("default", entity_id, resource, 0), "SK": sk_state()},
            UpdateExpression="SET #sched = :s, #tz = :tz, #none = :n",
            ExpressionAttributeNames={
                "#sched": BUCKET_FIELD_SCHED,
                "#tz": BUCKET_FIELD_SCHED_TZ,
                "#none": bucket_attr("tpm", BUCKET_FIELD_SCHED),
            },
            ExpressionAttributeValues={
                ":s": ALWAYS_HALF_COMPACT,
                ":tz": "UTC",
                ":n": BUCKET_SCHED_NONE,
            },
        )

        # The marker survives the round trip as an ordinary string attribute.
        assert _get_bucket(dynamodb_table, entity_id, resource)["b_tpm_sched"] == BUCKET_SCHED_NONE

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
                    sched=(),  # what the marker parses to
                ),
                "spm": LimitRefillInfo(
                    tc_delta=10_000_000,
                    tk_milli=0,
                    cp_milli=10_000_000,
                    ra_milli=10_000_000,
                    rp_ms=60_000,
                    sched=ALWAYS_HALF,
                ),
            },
            sched=ALWAYS_HALF,
            sched_compact=ALWAYS_HALF_COMPACT,
        )
        assert try_refill_bucket(dynamodb_table, state, now_ms) is True

        item = _get_bucket(dynamodb_table, entity_id, resource)
        assert int(item["b_tpm_tk"]) == 5_000_000  # 30s at the base rate
        assert int(item["b_spm_tk"]) == 2_500_000  # the 0.5x sibling on the same item


@pytest.mark.integration
class TestDurationWindowRollIntegration:
    """ADR-139 against real DynamoDB: the roll's positional aliases are legal.

    The shape is the sibling `_propagate_window_start` leaves behind: a window
    anchored on another shard (`ws > rf`) and `vu = 0`. The duration quota's
    name carries `.` and `-`, both legal in `NAME_PATTERN` and neither legal in
    an inline path or an expression token, so this is the part a MagicMock
    cannot validate. A dripping limit shares the item and is topped up by the
    same write.
    """

    SESSION = "s.q-1"
    RSA = 18_000

    def test_the_roll_lands_with_the_drip_and_a_later_image_is_refused(
        self, dynamodb_table
    ) -> None:
        entity_id = f"entity-{uuid.uuid4().hex[:8]}"
        resource = "gpt-4"
        now_ms = int(time.time() * 1000)
        old_rf_ms = now_ms - 30_000
        ws = now_ms - 10_000
        limits = {
            self.SESSION: {"tk": 0, "cp": 10_000_000, "ra": 0, "rp": 1_000, "tc": 0},
            "tpm": {"tk": 0, "cp": 10_000_000, "ra": 10_000_000, "rp": 60_000, "tc": 10_000_000},
        }
        _seed_bucket(dynamodb_table, entity_id, resource, limits=limits, rf_ms=old_rf_ms)
        dynamodb_table.update_item(
            Key={"PK": pk_bucket("default", entity_id, resource, 0), "SK": sk_state()},
            UpdateExpression="SET #ws = :ws, #rsa = :rsa, #vu = :zero",
            ExpressionAttributeNames={
                "#ws": bucket_attr(self.SESSION, BUCKET_FIELD_WS),
                "#rsa": bucket_attr(self.SESSION, BUCKET_FIELD_RSA),
                "#vu": BUCKET_FIELD_VU,
            },
            ExpressionAttributeValues={":ws": ws, ":rsa": self.RSA, ":zero": 0},
        )

        state = BucketRefillState(
            namespace_id="default",
            entity_id=entity_id,
            resource=resource,
            rf_ms=old_rf_ms,
            limits={
                self.SESSION: LimitRefillInfo(
                    tc_delta=0,
                    tk_milli=0,
                    cp_milli=10_000_000,
                    ra_milli=0,
                    rp_ms=1_000,
                    window_start_ms=ws,
                    reset_after_seconds=self.RSA,
                ),
                "tpm": LimitRefillInfo(
                    tc_delta=10_000_000,
                    tk_milli=0,
                    cp_milli=10_000_000,
                    ra_milli=10_000_000,
                    rp_ms=60_000,
                ),
            },
            vu_ms=0,
        )
        assert try_refill_bucket(dynamodb_table, state, now_ms) is True

        item = _get_bucket(dynamodb_table, entity_id, resource)
        assert int(item[bucket_attr(self.SESSION, BUCKET_FIELD_TK)]) == 10_000_000
        assert int(item["b_tpm_tk"]) == 5_000_000  # 30s at the base rate
        assert int(item["rf"]) == now_ms  # >= ws: the window is recorded as applied
        assert int(item[BUCKET_FIELD_VU]) == ws + self.RSA * 1000
        # The window itself is untouched: the aggregator never anchors one.
        assert int(item[bucket_attr(self.SESSION, BUCKET_FIELD_WS)]) == ws

        # The same stale image again: the rf lock refuses it, so the roll
        # cannot be applied twice.
        assert try_refill_bucket(dynamodb_table, state, now_ms + 1_000) is False
