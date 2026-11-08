"""Integration tests for the aggregator's bucket writes against real DynamoDB.

Tests verify that try_refill_bucket() works correctly with actual DynamoDB
(via LocalStack), including ADD commutativity with speculative writes and
the optimistic lock on the shared rf timestamp.

Since ADR-156 (#821) the aggregator never refills a dripping limit: every write
of one is exact, and the fast path reads a fill instant's balance at any
instant. What it writes is a quota's reset edge or duration-window roll, so the
properties below are checked on quotas, and a dripping limit is checked to be
left alone.

See: https://github.com/zeroae/zae-limiter/issues/317
"""

import os
import time
import uuid
from datetime import UTC, datetime

import boto3
import pytest

from zae_limiter.schedule import ScheduleEntry, decode, encode, encode_reset
from zae_limiter.schema import (
    BUCKET_FIELD_CP,
    BUCKET_FIELD_GC,
    BUCKET_FIELD_RA,
    BUCKET_FIELD_RP,
    BUCKET_FIELD_RSA,
    BUCKET_FIELD_RSCHED,
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


def _seed_bucket(
    table,
    entity_id: str,
    resource: str,
    limits: dict,
    rf_ms: int,
    shard_id: int = 0,
    rsched: str | None = None,
):
    """Seed a composite bucket item in its ``tk`` form (an item written before v0.18).

    Args:
        table: boto3 Table resource
        entity_id: Entity ID
        resource: Resource name
        limits: Dict of limit_name -> {tk, cp, ra, rp, tc} in millitokens/ms
        rf_ms: Shared refill timestamp
        shard_id: Shard index (default 0)
        rsched: The item's reset schedule (compact, UTC), for a quota
    """
    item = {
        "PK": pk_bucket("default", entity_id, resource, shard_id),
        "SK": sk_state(),
        "entity_id": entity_id,
        "rf": rf_ms,
        "shard_count": 1,
    }
    if rsched is not None:
        item[BUCKET_FIELD_RSCHED] = rsched
        item[BUCKET_FIELD_SCHED_TZ] = "UTC"
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
    rsched: str | None = None,
) -> dict:
    """Build a DynamoDB stream MODIFY record for a composite bucket.

    Args:
        entity_id: Entity ID
        resource: Resource name
        limits_old: Dict of limit_name -> {tk, cp, ra, rp, tc} for OldImage
        limits_new: Dict of limit_name -> {tk, cp, ra, rp, tc} for NewImage
        rf_ms: Shared refill timestamp in NewImage
        shard_id: Shard index (default 0)
        rsched: Both images' reset schedule (compact, UTC), for a quota
    """

    def _build_image(limits: dict, rf: int) -> dict:
        image = {
            "PK": {"S": pk_bucket("default", entity_id, resource, shard_id)},
            "SK": {"S": sk_state()},
            "entity_id": {"S": entity_id},
            "rf": {"N": str(rf)},
            "shard_count": {"N": "1"},
        }
        if rsched is not None:
            image[BUCKET_FIELD_RSCHED] = {"S": rsched}
            image[BUCKET_FIELD_SCHED_TZ] = {"S": "UTC"}
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


# A daily quota resetting at midnight UTC, last written an hour before one
# midnight and refilled half an hour after it: one reset edge in between. Fixed
# instants, so nothing below depends on the wall clock.
DAY_MS = 86_400_000
DAILY_RESET = (ScheduleEntry.reset(cron="0 0 * * *", tz="UTC"),)
DAILY_RESET_COMPACT = encode_reset(DAILY_RESET)[0]
MIDNIGHT = int(datetime(2027, 1, 16, tzinfo=UTC).timestamp() * 1000)
RF = MIDNIGHT - 3_600_000
NOW = MIDNIGHT + 1_800_000
# ADR-137's quota shape: no drip, an inert period.
RPD = {"tk": 2_000_000, "cp": 10_000_000, "ra": 0, "rp": 1_000, "tc": 8_000_000}


def _quota(tk: int, cp: int = 10_000_000, tc_delta: int = 0, **kwargs) -> LimitRefillInfo:
    """A daily quota as the aggregator parses it off a stream image."""
    return LimitRefillInfo(
        tc_delta=tc_delta,
        tk_milli=tk,
        cp_milli=cp,
        ra_milli=0,
        rp_ms=1_000,
        reset_sched=DAILY_RESET,
        **kwargs,
    )


def _quota_state(entity_id: str, resource: str, limits: dict, **kwargs) -> BucketRefillState:
    return BucketRefillState(
        namespace_id="default",
        entity_id=entity_id,
        resource=resource,
        rf_ms=RF,
        limits=limits,
        reset_sched=DAILY_RESET,
        **kwargs,
    )


@pytest.mark.integration
class TestTryRefillBucketIntegration:
    """Integration tests for try_refill_bucket against real DynamoDB."""

    def test_a_reset_writes_an_add(self, dynamodb_table) -> None:
        """A quota past its reset edge is restored by an ADD to its share."""
        entity_id = f"entity-{uuid.uuid4().hex[:8]}"
        resource = "gpt-4"
        _seed_bucket(
            dynamodb_table, entity_id, resource, {"rpd": RPD}, RF, rsched=DAILY_RESET_COMPACT
        )
        state = _quota_state(entity_id, resource, {"rpd": _quota(2_000_000)})

        assert try_refill_bucket(dynamodb_table, state, NOW) is True

        item = _get_bucket(dynamodb_table, entity_id, resource)
        assert item["b_rpd_tk"] == 10_000_000
        assert item["rf"] == NOW
        assert item[bucket_attr("rpd", BUCKET_FIELD_GC)] == 1  # granted at one shard

    def test_a_dripping_limit_is_never_written(self, dynamodb_table) -> None:
        """However depleted (ADR-156): the client's writes of one are exact."""
        entity_id = f"entity-{uuid.uuid4().hex[:8]}"
        resource = "gpt-4"
        old_rf_ms = int(time.time() * 1000) - 10_000
        tpm = {"tk": 0, "cp": 10_000_000, "ra": 10_000_000, "rp": 60_000, "tc": 10_000_000}
        _seed_bucket(dynamodb_table, entity_id, resource, {"tpm": tpm}, old_rf_ms)
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

        assert try_refill_bucket(dynamodb_table, state, int(time.time() * 1000)) is False

        item = _get_bucket(dynamodb_table, entity_id, resource)
        assert item["rf"] == old_rf_ms
        assert item["b_tpm_tk"] == 0

    def test_optimistic_lock_prevents_double_reset(self, dynamodb_table) -> None:
        """Second reset with stale rf fails gracefully (ConditionalCheckFailed)."""
        entity_id = f"entity-{uuid.uuid4().hex[:8]}"
        resource = "gpt-4"
        _seed_bucket(
            dynamodb_table, entity_id, resource, {"rpd": RPD}, RF, rsched=DAILY_RESET_COMPACT
        )
        state = _quota_state(entity_id, resource, {"rpd": _quota(2_000_000)})

        assert try_refill_bucket(dynamodb_table, state, NOW) is True
        # The same stale image again: `rf` has moved, so it is refused.
        assert try_refill_bucket(dynamodb_table, state, NOW + 1000) is False

        item = _get_bucket(dynamodb_table, entity_id, resource)
        assert item["b_rpd_tk"] == 10_000_000  # reset once

    def test_add_commutes_with_concurrent_consume(self, dynamodb_table) -> None:
        """The reset's ADD and a speculative debit's ADD commute.

        A debit lands after the stream image the aggregator works from: the
        reset still adds the delta it computed, and the debit stands.
        """
        entity_id = f"entity-{uuid.uuid4().hex[:8]}"
        resource = "gpt-4"
        _seed_bucket(
            dynamodb_table, entity_id, resource, {"rpd": RPD}, RF, rsched=DAILY_RESET_COMPACT
        )
        dynamodb_table.update_item(
            Key={"PK": pk_bucket("default", entity_id, resource, 0), "SK": sk_state()},
            UpdateExpression="ADD b_rpd_tk :consumed, b_rpd_tc :consumed_tc",
            ExpressionAttributeValues={":consumed": -1_000_000, ":consumed_tc": 1_000_000},
        )
        state = _quota_state(entity_id, resource, {"rpd": _quota(2_000_000)})  # the snapshot

        assert try_refill_bucket(dynamodb_table, state, NOW) is True

        item = _get_bucket(dynamodb_table, entity_id, resource)
        assert int(item["b_rpd_tk"]) == 2_000_000 - 1_000_000 + 8_000_000

    def test_multiple_limits_single_update(self, dynamodb_table) -> None:
        """Every quota due its reset is written in one UpdateItem."""
        entity_id = f"entity-{uuid.uuid4().hex[:8]}"
        resource = "gpt-4"
        tpd = {"tk": 0, "cp": 1_000_000, "ra": 0, "rp": 1_000, "tc": 1_000_000}
        _seed_bucket(
            dynamodb_table,
            entity_id,
            resource,
            {"rpd": RPD, "tpd": tpd},
            RF,
            rsched=DAILY_RESET_COMPACT,
        )
        state = _quota_state(
            entity_id,
            resource,
            {"rpd": _quota(2_000_000), "tpd": _quota(0, cp=1_000_000)},
        )

        assert try_refill_bucket(dynamodb_table, state, NOW) is True

        item = _get_bucket(dynamodb_table, entity_id, resource)
        assert int(item["b_rpd_tk"]) == 10_000_000
        assert int(item["b_tpd_tk"]) == 1_000_000
        assert item["rf"] == NOW


@pytest.mark.integration
class TestAggregateAndRefillIntegration:
    """End-to-end: stream records → aggregate → reset → verify DynamoDB."""

    def test_stream_to_refill_pipeline(self, dynamodb_table) -> None:
        """Full pipeline: parse stream records, aggregate, and reset the quota."""
        entity_id = f"entity-{uuid.uuid4().hex[:8]}"
        resource = "claude-3"

        def rpd(tk: int, tc: int) -> dict:
            return {"rpd": {"tk": tk, "cp": 10_000_000, "ra": 0, "rp": 1_000, "tc": tc}}

        _seed_bucket(
            dynamodb_table,
            entity_id,
            resource,
            rpd(0, 20_000_000),
            RF,
            rsched=DAILY_RESET_COMPACT,
        )
        # Two speculative writes: 3k, then 2k more.
        records = [
            _make_stream_record(
                entity_id,
                resource,
                rpd(5_000_000, 15_000_000),
                rpd(2_000_000, 18_000_000),
                RF,
                rsched=DAILY_RESET_COMPACT,
            ),
            _make_stream_record(
                entity_id,
                resource,
                rpd(2_000_000, 18_000_000),
                rpd(0, 20_000_000),
                RF,
                rsched=DAILY_RESET_COMPACT,
            ),
        ]

        bucket_states = aggregate_bucket_states(records)
        assert len(bucket_states) == 1
        state = bucket_states[("default", entity_id, resource, 0)]
        assert state.limits["rpd"].tc_delta == 5_000_000  # across both events
        assert state.limits["rpd"].tk_milli == 0  # the last NewImage

        assert try_refill_bucket(dynamodb_table, state, NOW) is True

        item = _get_bucket(dynamodb_table, entity_id, resource)
        assert int(item["b_rpd_tk"]) == 10_000_000
        assert item["rf"] == NOW


# Always active, so the assertions below do not depend on the wall clock.
ALWAYS_HALF = (ScheduleEntry(cron="* * * * *", tz="UTC", scale=0.5),)
ALWAYS_HALF_COMPACT, _ALWAYS_HALF_TZ = encode(ALWAYS_HALF)
ALWAYS_QUARTER_COMPACT = encode((ScheduleEntry(cron="* * * * *", tz="UTC", scale=0.25),))[0]


@pytest.mark.integration
class TestScheduledRefillIntegration:
    """A scheduled quota's reset and `vu` re-stamping against real DynamoDB (#222).

    The unit tests assert what is *passed* to update_item. These assert that
    DynamoDB accepts it: the combined ``SET rf, #vu ADD ...`` expression and the
    two-clause condition are the parts a MagicMock cannot validate.
    """

    def _seed(self, table, entity_id, resource, *, sched, vu_ms):
        empty = {"tk": 0, "cp": 10_000_000, "ra": 0, "rp": 1_000, "tc": 10_000_000}
        _seed_bucket(table, entity_id, resource, {"rpd": empty}, RF, rsched=DAILY_RESET_COMPACT)
        table.update_item(
            Key={"PK": pk_bucket("default", entity_id, resource, 0), "SK": sk_state()},
            UpdateExpression="SET #sched = :s, #vu = :vu",
            ExpressionAttributeNames={"#sched": BUCKET_FIELD_SCHED, "#vu": BUCKET_FIELD_VU},
            ExpressionAttributeValues={":s": sched, ":vu": vu_ms},
        )

    def _state(self, entity_id, resource, *, sched_compact, vu_ms):
        sched = decode(sched_compact, "UTC")
        return _quota_state(
            entity_id,
            resource,
            {"rpd": _quota(0, sched=sched)},
            sched=sched,
            sched_compact=sched_compact,
            vu_ms=vu_ms,
        )

    def test_refills_to_the_scheduled_share_and_advances_vu(self, dynamodb_table) -> None:
        entity_id = f"entity-{uuid.uuid4().hex[:8]}"
        resource = "gpt-4"
        self._seed(dynamodb_table, entity_id, resource, sched=ALWAYS_HALF_COMPACT, vu_ms=MIDNIGHT)
        state = self._state(entity_id, resource, sched_compact=ALWAYS_HALF_COMPACT, vu_ms=MIDNIGHT)

        assert try_refill_bucket(dynamodb_table, state, NOW) is True

        item = _get_bucket(dynamodb_table, entity_id, resource)
        assert item["b_rpd_tk"] == 5_000_000, "reset to the halved share"
        assert item["rf"] == NOW
        assert item[BUCKET_FIELD_VU] > NOW, "vu must advance past now"

    def test_a_schedule_changed_underneath_blocks_the_whole_write(self, dynamodb_table) -> None:
        """The #468 fan-out rewrites `sched` and sets `vu = 0` without touching
        `rf`, so the rf lock alone cannot tell a pre-fan-out stream image from a
        current one. Re-stamping `vu` off the stale image would push it back
        into the future and cancel the materialising pass `vu = 0` forced."""
        entity_id = f"entity-{uuid.uuid4().hex[:8]}"
        resource = "gpt-4"
        # The item is on the 0.25x schedule; the stream image the aggregator
        # is working from still says 0.5x.
        self._seed(dynamodb_table, entity_id, resource, sched=ALWAYS_QUARTER_COMPACT, vu_ms=0)
        state = self._state(entity_id, resource, sched_compact=ALWAYS_HALF_COMPACT, vu_ms=0)

        assert try_refill_bucket(dynamodb_table, state, NOW) is False

        item = _get_bucket(dynamodb_table, entity_id, resource)
        assert item["rf"] == RF, "nothing was written"
        assert item[BUCKET_FIELD_VU] == 0, "the forced pass is still pending"

    def test_the_same_write_lands_when_the_schedule_still_matches(self, dynamodb_table) -> None:
        """Discriminates the test above: identical setup, current image."""
        entity_id = f"entity-{uuid.uuid4().hex[:8]}"
        resource = "gpt-4"
        self._seed(dynamodb_table, entity_id, resource, sched=ALWAYS_QUARTER_COMPACT, vu_ms=0)
        state = self._state(entity_id, resource, sched_compact=ALWAYS_QUARTER_COMPACT, vu_ms=0)

        assert try_refill_bucket(dynamodb_table, state, NOW) is True

        item = _get_bucket(dynamodb_table, entity_id, resource)
        assert item["b_rpd_tk"] == 2_500_000
        assert item[BUCKET_FIELD_VU] > NOW


@pytest.mark.integration
class TestMixedScheduleItemIntegration:
    """#541 against real DynamoDB: the marker round-trips and is honoured.

    ``BUCKET_SCHED_NONE`` is a reserved string attribute value, so this is the
    part a MagicMock cannot validate — that DynamoDB stores ``"-"`` and returns
    it unchanged in a stream image, and that the reset it drives lands.

    ``rpd`` is unscheduled and ``spd`` shares the item's always-on ``0.5x``
    window. Both are empty and due their daily reset, so the two deltas
    separate the readings exactly: the base 10,000,000 and the halved
    5,000,000.
    """

    LIMITS = {
        "rpd": {"tk": 0, "cp": 10_000_000, "ra": 0, "rp": 1_000, "tc": 10_000_000},
        "spd": {"tk": 0, "cp": 10_000_000, "ra": 0, "rp": 1_000, "tc": 10_000_000},
    }

    def test_an_unscheduled_limit_refills_at_its_base_rate(self, dynamodb_table) -> None:
        entity_id = f"entity-{uuid.uuid4().hex[:8]}"
        resource = "gpt-4"
        _seed_bucket(
            dynamodb_table, entity_id, resource, self.LIMITS, RF, rsched=DAILY_RESET_COMPACT
        )
        dynamodb_table.update_item(
            Key={"PK": pk_bucket("default", entity_id, resource, 0), "SK": sk_state()},
            UpdateExpression="SET #sched = :s, #none = :n",
            ExpressionAttributeNames={
                "#sched": BUCKET_FIELD_SCHED,
                "#none": bucket_attr("rpd", BUCKET_FIELD_SCHED),
            },
            ExpressionAttributeValues={":s": ALWAYS_HALF_COMPACT, ":n": BUCKET_SCHED_NONE},
        )

        # The marker survives the round trip as an ordinary string attribute.
        assert _get_bucket(dynamodb_table, entity_id, resource)["b_rpd_sched"] == BUCKET_SCHED_NONE

        state = _quota_state(
            entity_id,
            resource,
            {
                "rpd": _quota(0, sched=()),  # what the marker parses to
                "spd": _quota(0, sched=ALWAYS_HALF),
            },
            sched=ALWAYS_HALF,
            sched_compact=ALWAYS_HALF_COMPACT,
        )
        assert try_refill_bucket(dynamodb_table, state, NOW) is True

        item = _get_bucket(dynamodb_table, entity_id, resource)
        assert int(item["b_rpd_tk"]) == 10_000_000  # the base share
        assert int(item["b_spd_tk"]) == 5_000_000  # the 0.5x sibling on the same item


@pytest.mark.integration
class TestDurationWindowRollIntegration:
    """ADR-139 against real DynamoDB: the roll's positional aliases are legal.

    The shape is the sibling `_propagate_window_start` leaves behind: a window
    anchored on another shard (`ws > rf`) and `vu = 0`. The duration quota's
    name carries `.` and `-`, both legal in `NAME_PATTERN` and neither legal in
    an inline path or an expression token, so this is the part a MagicMock
    cannot validate. A dripping limit shares the item and is left alone by the
    same write (ADR-156).
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
        assert int(item["b_tpm_tk"]) == 0  # a dripping limit is never refilled here
        assert int(item["rf"]) == now_ms  # >= ws: the window is recorded as applied
        assert int(item[BUCKET_FIELD_VU]) == ws + self.RSA * 1000
        # The window itself is untouched: the aggregator never anchors one.
        assert int(item[bucket_attr(self.SESSION, BUCKET_FIELD_WS)]) == ws

        # The same stale image again: the rf lock refuses it, so the roll
        # cannot be applied twice.
        assert try_refill_bucket(dynamodb_table, state, now_ms + 1_000) is False

    def test_a_roll_is_refused_once_the_next_window_is_fanned_out(self, dynamodb_table) -> None:
        """The image shows W1 unapplied with `vu = 0`. Before the aggregator
        writes, a client fans W2 out the way `_propagate_window_start` does —
        `SET ws, rsa, vu = 0`, `rf` untouched — so the `rf` and `vu` pins
        still match. Only the `ws` pin tells the two windows apart."""
        entity_id = f"entity-{uuid.uuid4().hex[:8]}"
        resource = "gpt-4"
        now_ms = int(time.time() * 1000)
        old_rf_ms = now_ms - 30_000
        ws = now_ms - 10_000
        limits = {self.SESSION: {"tk": 7, "cp": 10_000_000, "ra": 0, "rp": 1_000, "tc": 0}}
        _seed_bucket(dynamodb_table, entity_id, resource, limits=limits, rf_ms=old_rf_ms)
        key = {"PK": pk_bucket("default", entity_id, resource, 0), "SK": sk_state()}
        fan_out = {
            "UpdateExpression": "SET #ws = :ws, #rsa = :rsa, #vu = :zero",
            "ExpressionAttributeNames": {
                "#ws": bucket_attr(self.SESSION, BUCKET_FIELD_WS),
                "#rsa": bucket_attr(self.SESSION, BUCKET_FIELD_RSA),
                "#vu": BUCKET_FIELD_VU,
            },
        }
        dynamodb_table.update_item(
            Key=key, ExpressionAttributeValues={":ws": ws, ":rsa": self.RSA, ":zero": 0}, **fan_out
        )
        state = BucketRefillState(
            namespace_id="default",
            entity_id=entity_id,
            resource=resource,
            rf_ms=old_rf_ms,
            limits={
                self.SESSION: LimitRefillInfo(
                    tc_delta=0,
                    tk_milli=7,
                    cp_milli=10_000_000,
                    ra_milli=0,
                    rp_ms=1_000,
                    window_start_ms=ws,
                    reset_after_seconds=self.RSA,
                )
            },
            vu_ms=0,
        )

        # W2 lands after the image was taken.
        dynamodb_table.update_item(
            Key=key,
            ExpressionAttributeValues={":ws": ws + 5_000, ":rsa": self.RSA, ":zero": 0},
            **fan_out,
        )

        assert try_refill_bucket(dynamodb_table, state, now_ms) is False
        item = _get_bucket(dynamodb_table, entity_id, resource)
        assert int(item[bucket_attr(self.SESSION, BUCKET_FIELD_TK)]) == 7
        assert int(item["rf"]) == old_rf_ms
