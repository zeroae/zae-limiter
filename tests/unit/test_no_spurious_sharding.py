"""Steady traffic far below the partition write ceiling must never shard.

`wcu` is 1000 writes/s per shard. 100 concurrent acquires a second is a tenth
of that, so neither the client nor the aggregator has any reason to double
`shard_count`. Before the fix the aggregator did anyway: it read the stored
`wcu` balance off the stream image, which the fast path drains by one token per
write and never refills, so ~800 writes over any period read as a hot
partition — and its own doubling write fed the next doubling without bound.

The client runs on a simulated clock (`Repository._now_ms`) and the aggregator
is fed the table's real DynamoDB stream, batched as the event source mapping
would batch it, on the same clock.
"""

import asyncio
from unittest.mock import MagicMock, patch

import boto3
import pytest

from zae_limiter import Limit
from zae_limiter_aggregator.processor import process_stream_records

TABLE = "test-rate-limits"
RESOURCE = "api"
ENTITY = "user-1"
CONCURRENCY = 100  # acquires in flight each simulated second
DURATION_S = 120
AGGREGATOR_EVERY_S = 5  # MaximumBatchingWindowInSeconds in the CFN template
BATCH_SIZE = 100  # BatchSize in the CFN template


class _Stream:
    """Reads the moto table's stream from TRIM_HORIZON, in event-source batches."""

    def __init__(self) -> None:
        client = boto3.client("dynamodb", region_name="us-east-1")
        table = client.describe_table(TableName=TABLE)["Table"]
        assert table["StreamSpecification"]["StreamViewType"] == "NEW_AND_OLD_IMAGES"
        arn = table["LatestStreamArn"]
        self._streams = boto3.client("dynamodbstreams", region_name="us-east-1")
        shard = self._streams.describe_stream(StreamArn=arn)["StreamDescription"]["Shards"][0]
        self._iterator = self._streams.get_shard_iterator(
            StreamArn=arn, ShardId=shard["ShardId"], ShardIteratorType="TRIM_HORIZON"
        )["ShardIterator"]

    def drain(self) -> list[list[dict]]:
        batches = []
        while True:
            resp = self._streams.get_records(ShardIterator=self._iterator, Limit=BATCH_SIZE)
            self._iterator = resp["NextShardIterator"]
            if not resp["Records"]:
                return batches
            batches.append(resp["Records"])


@pytest.mark.slow  # ~100 s on moto: 12,000 acquires
async def test_steady_traffic_below_the_write_ceiling_never_shards(limiter, monkeypatch) -> None:
    stream = _Stream()
    repo = limiter._repository
    clock = {"ms": repo._now_ms()}
    monkeypatch.setattr(repo, "_now_ms", lambda: clock["ms"])
    aggregator_clock = MagicMock()
    aggregator_clock.time.side_effect = lambda: clock["ms"] / 1000
    aggregator_clock.perf_counter.return_value = 0.0

    await limiter.set_resource_defaults(
        RESOURCE,
        [Limit.custom("rpm", capacity=200_000, refill_amount=200_000, refill_period_seconds=3600)],
    )
    await limiter.create_entity(ENTITY)

    async def one_acquire() -> None:
        async with limiter.acquire(ENTITY, RESOURCE, {"rpm": 1}):
            pass

    records_seen = 0
    for second in range(1, DURATION_S + 1):
        await asyncio.gather(*[one_acquire() for _ in range(CONCURRENCY)])
        clock["ms"] += 1000
        if second % AGGREGATOR_EVERY_S == 0:
            with patch("zae_limiter_aggregator.processor.time_module", aggregator_clock):
                for batch in stream.drain():
                    records_seen += len(batch)
                    result = process_stream_records(batch, TABLE, windows=[])
                    assert result.errors == []

    # The aggregator really ran over the traffic, so the assertion below is not vacuous.
    assert records_seen >= CONCURRENCY * DURATION_S

    buckets = await repo.get_buckets(ENTITY)
    shard_items = {
        item["PK"]: item
        for item in boto3.resource("dynamodb", region_name="us-east-1").Table(TABLE).scan()["Items"]
        if item["PK"].startswith(f"{repo._namespace_id}/BUCKET#{ENTITY}#{RESOURCE}#")
    }
    assert list(shard_items) == [f"{repo._namespace_id}/BUCKET#{ENTITY}#{RESOURCE}#0"]
    assert int(next(iter(shard_items.values())).get("shard_count", 1)) == 1
    assert {b.shard_count for b in buckets} == {1}
    # Every acquire was admitted: 12,000 against an allowance of 200,000.
    rpm = next(b for b in buckets if b.limit_name == "rpm")
    assert rpm.total_consumed_milli == CONCURRENCY * DURATION_S * 1000
